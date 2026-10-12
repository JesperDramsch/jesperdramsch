"""Generate the projects and contributions block of the GitHub profile README.

The block between the PROFILE-STATS markers in README.md is rewritten from live
GitHub data. Nothing in it is typed by hand, so it cannot go stale.

Own repositories get a repostatus.org state (https://www.repostatus.org/):

* Archived repositories are Unsupported, or Moved when profile.toml names a
  successor. The archive flag always wins.
* Otherwise a declared state wins: a repostatus badge in the README, then a
  .repostatus.org or repostatus.org file, then developmentStatus in codemeta.json.
* Otherwise activity decides, and it can only promote: Active needs at least one
  owner event in the last 90 days and at least ten in the last 365 days. WIP is a
  repository created in the last year with recent activity below that bar.

Owner events are commits by the owner (or co-authored by them, or by an AI
assistant in the owner's own repositories), PR reviews, issue comments and issue
closures. Bot accounts never count.

Contributions to repositories the owner does not own are grouped per
organisation. An organisation is current when it saw merged PRs, reviews or
commits in at least three of the last four 13-week windows, the latest within 26
weeks. Others are listed as earlier contributions when they reach ten merged PRs
plus reviewed PRs over all time.

If any required data is missing the script exits non-zero and leaves the README
untouched. A report for the owner goes to the Actions job summary.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

API = "https://api.github.com"
START_MARKER = "<!-- PROFILE-STATS:START -->"
END_MARKER = "<!-- PROFILE-STATS:END -->"

# Activity rule for own repositories: the 90-day window follows OpenSSF Scorecard,
# the yearly count follows Miller et al. (ICSE 2025).
RECENT_DAYS = 90
YEAR_DAYS = 365
ACTIVE_MIN_YEAR_EVENTS = 10
WIP_MAX_AGE_DAYS = 365
# Flag a declared Inactive repository with this many events in RECENT_DAYS.
CONTRADICTION_RECENT_EVENTS = 10

# Contribution rule for organisations.
ORG_WINDOW_DAYS = 91
ORG_WINDOWS = 4
ORG_MIN_ACTIVE_WINDOWS = 3
ORG_RECENT_DAYS = 182
ORG_COUNT_DAYS = 730
PAST_MIN_CONTRIBUTIONS = 10

REPOSTATUS_STATES = {
    "concept",
    "wip",
    "suspended",
    "abandoned",
    "active",
    "inactive",
    "unsupported",
    "moved",
}
REPOSTATUS_BADGE = re.compile(
    r"https?://[^\s)\"'<>]*repostatus\.org/badges/[^/\s]+/([a-z]+)\.svg"
)
# Lexicographic order, as the repostatus reference parsers search README files.
README_FIELDS = ("readmePlain", "readmeMd", "readmeRst", "readmeLower")

SECTIONS = (
    ("active", "Actively developed"),
    ("wip", "In progress"),
    ("inactive", "Finished, maintained as time allows"),
    ("unsupported", "No longer maintained"),
)
NOTE_TOPICS = (
    ("workshop", "workshop material"),
    ("course", "teaching"),
    ("tutorial", "teaching"),
)
# CITATION.cff types that describe software or data; they veto "paper code".
SOFTWARE_DATA_TYPES = {
    "software",
    "software-code",
    "software-container",
    "software-executable",
    "software-virtual-machine",
    "data",
    "database",
}
# Types that are not a publication by themselves but do not veto a DOI.
NEUTRAL_TYPES = {"generic", "website", "slides", "video"}


class DataError(RuntimeError):
    """Raised when GitHub data is missing or implausible; the README stays untouched."""


@dataclasses.dataclass
class Config:
    owner: str
    projects_url: str
    max_entries: int
    names: list[str]
    assistant_emails: list[str]
    org_names: dict[str, str]
    moved: dict[str, str]
    org_aliases: dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        identities = raw.get("identities", {})
        return cls(
            owner=raw["owner"],
            projects_url=raw["projects_url"],
            max_entries=int(raw.get("max_entries", 5)),
            names=list(identities.get("names", [])),
            assistant_emails=[
                e.lower() for e in identities.get("assistant_emails", [])
            ],
            org_names=dict(raw.get("org_names", {})),
            moved=dict(raw.get("moved", {})),
            org_aliases=dict(raw.get("org_aliases", {})),
        )


@dataclasses.dataclass
class Repo:
    name: str
    url: str
    description: str
    fork: bool
    archived: bool
    stars: int
    created: dt.date
    pushed: dt.date
    topics: list[str]
    declared: str | None
    paper_citation: bool
    default_branch: str | None
    parent: str | None = None
    parent_branch: str | None = None
    moved_to: str | None = None
    events: list[dt.date] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class OrgActivity:
    login: str
    owner_type: str = "Organization"
    events: list[dt.date] = dataclasses.field(default_factory=list)
    merged_recent: int = 0
    reviewed_recent: int = 0
    total: int = 0
    repos_recent: Counter = dataclasses.field(default_factory=Counter)
    repos_total: Counter = dataclasses.field(default_factory=Counter)


# --- parsing ---


def parse_date(value: str) -> dt.date:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def state_from_text(text: str | None) -> str | None:
    """Return the repostatus state named by the first badge URL in text, if any."""
    if not text:
        return None
    for match in REPOSTATUS_BADGE.finditer(text):
        if match.group(1) in REPOSTATUS_STATES:
            return match.group(1)
    return None


def state_from_codemeta(text: str | None) -> str | None:
    """Return developmentStatus from codemeta.json as a bare repostatus id."""
    if not text:
        return None
    try:
        value = json.loads(text.lstrip("\ufeff")).get("developmentStatus")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(value, str):
        return None
    candidate = re.split(r"[#/]", value.strip().lower())[-1]
    return candidate if candidate in REPOSTATUS_STATES else None


def declared_state(
    readmes: list[str | None], status_files: list[str | None], codemeta: str | None
) -> str | None:
    for text in [*readmes, *status_files]:
        state = state_from_text(text)
        if state:
            return state
    return state_from_codemeta(codemeta)


def citation_is_publication(cff: str | None) -> bool:
    """True when CITATION.cff prefers citing a publication (paper, chapter, thesis).

    Only the direct keys of the preferred-citation block are read, so the type of
    a nested reference does not leak in.
    """
    if not cff:
        return False
    # Comment-only lines carry no structure; drop them before reading the block.
    lines = [line for line in cff.splitlines() if not line.lstrip().startswith("#")]
    try:
        start = next(
            i
            for i, line in enumerate(lines)
            if re.match(r"^preferred-citation:\s*(#.*)?$", line)
        )
    except StopIteration:
        return False
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line[0].isspace():
            break
        block.append(line)
    indents = [
        len(line) - len(line.lstrip())
        for line in block
        if line.strip() and not line.lstrip().startswith("-")
    ]
    if not indents:
        return False
    base = min(indents)
    keys: dict[str, str] = {}
    for line in block:
        if line.strip() and len(line) - len(line.lstrip()) == base and ":" in line:
            key, _, value = line.strip().partition(":")
            value = re.sub(r"\s+#.*$", "", value.strip())
            keys[key.strip()] = value.strip("'\"").lower()
    kind = keys.get("type")
    if kind in SOFTWARE_DATA_TYPES:
        return False
    return bool(keys.get("doi")) or bool(kind and kind not in NEUTRAL_TYPES)


def co_author_names(message: str) -> list[str]:
    return [
        m.group(1).strip()
        for m in re.finditer(r"^co-authored-by:\s*([^<\n]+?)\s*<", message, re.I | re.M)
    ]


def is_bot(login: str | None, name: str | None) -> bool:
    return bool(login and login.endswith("[bot]")) or bool(
        name and name.endswith("[bot]")
    )


def is_owner_commit(commit: dict[str, Any], config: Config, owned_repo: bool) -> bool:
    """Decide whether a REST commit object counts as an owner event."""
    login = (commit.get("author") or {}).get("login")
    author = (commit.get("commit") or {}).get("author") or {}
    name, email = author.get("name") or "", (author.get("email") or "").lower()
    if is_bot(login, name):
        return False
    if login == config.owner or name in config.names:
        return True
    if any(
        n in config.names
        for n in co_author_names((commit.get("commit") or {}).get("message", ""))
    ):
        return True
    return owned_repo and email in config.assistant_emails


# --- rules ---


def classify(repo: Repo, today: dt.date) -> str | None:
    if repo.archived:
        return "moved" if repo.moved_to else "unsupported"
    if repo.declared:
        return repo.declared
    recent = sum(1 for d in repo.events if (today - d).days <= RECENT_DAYS)
    year = sum(1 for d in repo.events if (today - d).days <= YEAR_DAYS)
    if recent >= 1 and year >= ACTIVE_MIN_YEAR_EVENTS:
        return "active"
    if recent >= 1 and (today - repo.created).days <= WIP_MAX_AGE_DAYS:
        return "wip"
    return None


def contradiction(repo: Repo, today: dt.date) -> str | None:
    if repo.archived or not repo.declared:
        return None
    recent = sum(1 for d in repo.events if (today - d).days <= RECENT_DAYS)
    year = sum(1 for d in repo.events if (today - d).days <= YEAR_DAYS)
    if repo.declared == "active" and year == 0:
        return "declared Active but no owner events in 365 days"
    if repo.declared == "inactive" and recent >= CONTRADICTION_RECENT_EVENTS:
        return (
            f"declared Inactive but {recent} owner events "
            f"in the last {RECENT_DAYS} days"
        )
    return None


def active_windows(events: list[dt.date], today: dt.date) -> int:
    windows = set()
    for d in events:
        age = (today - d).days
        if 0 <= age < ORG_WINDOW_DAYS * ORG_WINDOWS:
            windows.add(age // ORG_WINDOW_DAYS)
    return len(windows)


def org_is_current(org: OrgActivity, today: dt.date) -> bool:
    if not org.events:
        return False
    latest = max(org.events)
    # With 3 of 4 windows active, window 0 or 1 always is, so the latest event is
    # at most 181 days old. The recency clause states the agreed rule explicitly
    # and keeps it true if the window settings change.
    return (
        active_windows(org.events, today) >= ORG_MIN_ACTIVE_WINDOWS
        and (today - latest).days <= ORG_RECENT_DAYS
    )


def note_for(repo: Repo) -> str | None:
    if repo.paper_citation:
        return "paper code"
    for topic, note in NOTE_TOPICS:
        if topic in repo.topics:
            return note
    return None


def round_down(n: int, step: int = 10) -> int:
    return n // step * step


# --- rendering ---


def plural(n: int, word: str, words: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (words or word + 's')}"


def clean_description(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().rstrip(".!")


def repo_line(repo: Repo, state: str, today: dt.date) -> str:
    parts = [f"[{repo.name}]({repo.url})"]
    detail = clean_description(repo.description)
    if state == "inactive" and (note := note_for(repo)):
        detail = f"{detail} ({note})" if detail else f"({note})"
    if state in ("active", "wip"):
        year = sum(1 for d in repo.events if (today - d).days <= YEAR_DAYS)
        fact = f"{plural(year, 'contribution')} in the last year"
    else:
        fact = plural(repo.stars, "star")
    text = parts[0] + (f": {detail}, {fact}" if detail else f": {fact}")
    return f"- {text}"


def sort_key(repo: Repo, state: str, today: dt.date) -> tuple:
    if state in ("active", "wip"):
        return (
            -sum(1 for d in repo.events if (today - d).days <= YEAR_DAYS),
            repo.name.lower(),
        )
    return (-repo.stars, repo.name.lower())


def org_label(org: OrgActivity, config: Config, recent: bool) -> tuple[str, str]:
    counter = org.repos_recent if recent and org.repos_recent else org.repos_total
    if org.login in config.org_names:
        return config.org_names[org.login], f"https://github.com/{org.login}"
    if org.owner_type == "User" and counter:
        busiest = counter.most_common(1)[0][0]
        return busiest.split("/", 1)[1], f"https://github.com/{busiest}"
    return org.login, f"https://github.com/{org.login}"


def render_block(
    states: dict[str, list[Repo]],
    current: list[OrgActivity],
    past: list[OrgActivity],
    private_count: int,
    config: Config,
    today: dt.date,
) -> str:
    out: list[str] = []
    for state, heading in SECTIONS:
        repos = sorted(states.get(state, []), key=lambda r: sort_key(r, state, today))
        if not repos:
            continue
        out += [f"**{heading}**", ""]
        out += [repo_line(r, state, today) for r in repos[: config.max_entries]]
        if len(repos) > config.max_entries:
            more = len(repos) - config.max_entries
            out.append(
                f"- and {more} more at "
                f"[{config.projects_url.split('://', 1)[-1]}]({config.projects_url})"
            )
        out.append("")
    contributions: list[str] = []
    for org in current:
        name, url = org_label(org, config, recent=True)
        line = (
            f"- Currently contributing to [{name}]({url}): "
            f"{plural(org.merged_recent, 'merged pull request')} and "
            f"{plural(org.reviewed_recent, 'review')} in the last two years"
        )
        if org.owner_type != "User" and org.repos_recent:
            busiest = org.repos_recent.most_common(1)[0][0]
            line += (
                f", most in [{busiest.split('/', 1)[1]}](https://github.com/{busiest})"
            )
        contributions.append(line)
    if past:
        items = []
        for org in past:
            name, url = org_label(org, config, recent=False)
            items.append(f"[{name}]({url}) ({org.total:,})")
        joined = (
            items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        )
        contributions.append(f"- Earlier: {joined} merged pull requests and reviews")
    if private_count >= 10:
        contributions.append(
            f"- Plus about {round_down(private_count):,} contributions "
            "to private repositories in the last year"
        )
    if contributions:
        out += ["**Contributions**", "", *contributions, ""]
    return "\n".join([START_MARKER, *out, END_MARKER])


def replace_block(readme: str, block: str) -> str:
    start, end = readme.find(START_MARKER), readme.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise DataError("README.md is missing the PROFILE-STATS markers")
    return readme[:start] + block + readme[end + len(END_MARKER) :]


# --- GitHub client ---


def rate_limit_wait(
    retry_after: str | None, reset: str | None, now: float | None = None
) -> int:
    """Seconds to wait before retrying a rate-limited request."""
    now = time.time() if now is None else now
    if retry_after:
        try:
            return int(retry_after)
        except ValueError:
            pass
    try:
        return max(0, int(reset or 0) - int(now)) + 1
    except ValueError:
        return 60


class GitHub:
    """Minimal REST/GraphQL client with SSO fallback and rate-limit handling."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.calls = 0
        self.sso_fallbacks = 0
        self.capped_searches: list[str] = []
        self._last_search = 0.0

    def _open(
        self, url: str, data: bytes | None, auth: bool
    ) -> tuple[Any, dict[str, str]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "profile-stats",
        }
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        self.calls += 1
        with urllib.request.urlopen(request, timeout=60) as response:
            # Header names are case-insensitive; normalise them for lookups.
            return json.load(response), {
                k.lower(): v for k, v in response.headers.items()
            }

    def request(
        self, url: str, data: bytes | None = None, auth: bool = True
    ) -> tuple[Any, dict[str, str]]:
        for attempt in range(4):
            try:
                body, headers = self._open(url, data, auth)
            except urllib.error.HTTPError as err:
                text = err.read().decode("utf-8", "replace")
                sso = (
                    "x-github-sso" in {k.lower() for k in err.headers} or "SAML" in text
                )
                if auth and self.token and sso and err.code in (403, 404, 422):
                    # The owner's token may be blocked by an organisation's SSO;
                    # public data is still readable without it.
                    self.sso_fallbacks += 1
                    auth = False
                    continue
                retry_after = err.headers.get("retry-after")
                if err.code in (403, 429) and (
                    retry_after or err.headers.get("x-ratelimit-remaining") == "0"
                ):
                    wait = rate_limit_wait(
                        retry_after, err.headers.get("x-ratelimit-reset")
                    )
                    if wait <= 90 and attempt < 3:
                        time.sleep(wait)
                        continue
                if err.code in (502, 503, 504) and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise DataError(
                    f"GitHub API {err.code} for {url}: {text[:200]}"
                ) from err
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                json.JSONDecodeError,
            ) as err:
                # All requests are reads, so retrying them is safe.
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise DataError(f"GitHub API unreachable for {url}: {err}") from err
            if (
                auth
                and self.token
                and "partial-results" in headers.get("x-github-sso", "")
            ):
                self.sso_fallbacks += 1
                auth = False
                continue
            return body, headers
        raise DataError(f"GitHub API kept failing for {url}")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self.request(url)[0]

    def paginate(
        self, path: str, params: dict[str, Any], max_pages: int = 10
    ) -> list[Any]:
        url: str | None = f"{API}{path}?" + urllib.parse.urlencode(
            {**params, "per_page": 100}
        )
        items: list[Any] = []
        for _ in range(max_pages):
            if not url:
                break
            body, headers = self.request(url)
            items.extend(body)
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link", ""))
            url = match.group(1) if match else None
        return items

    @staticmethod
    def public_query(query: str) -> str:
        """Restrict a search to public repositories.

        A personal token also sees private repositories; the profile is public, so
        nothing private may be counted or named.
        """
        return query if "is:public" in query.split() else f"{query} is:public"

    def _search_page(self, kind: str, query: str, page: int) -> dict[str, Any]:
        # A stable sort keeps pages from overlapping while results change.
        sort = {"commits": "committer-date", "issues": "created"}[kind]
        params = {"q": query, "per_page": 100, "page": page, "sort": sort}
        for attempt in range(3):
            # Search allows 30 requests a minute with a token and 10 without.
            gap = 2.2 if self.token else 6.5
            time.sleep(max(0.0, self._last_search + gap - time.time()))
            self._last_search = time.time()
            body = self.get(f"/search/{kind}", {**params, "order": "asc"})
            if not body.get("incomplete_results"):
                return body
            time.sleep(2 ** (attempt + 1))
        raise DataError(f"search timed out with incomplete results: {query!r}")

    def search(
        self, kind: str, query: str, max_pages: int = 10
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return the total count and all items, failing on short results.

        GitHub serves at most 1,000 results per search; hitting that cap is
        recorded for the report rather than treated as an error.
        """
        query = self.public_query(query)
        items: dict[str, dict[str, Any]] = {}
        total = 0
        for page in range(1, max_pages + 1):
            body = self._search_page(kind, query, page)
            total = body["total_count"]
            for item in body["items"]:
                items[str(item.get("id") or item.get("sha"))] = item
            if len(body["items"]) < 100 or len(items) >= min(total, 1000):
                break
        expected = min(total, 1000)
        # Allow a couple of items to appear or vanish while paging.
        if len(items) < expected - 2:
            raise DataError(
                f"search returned {len(items)} of {expected} items: {query!r}"
            )
        if total > 1000:
            self.capped_searches.append(query)
        return total, list(items.values())

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise DataError("GraphQL needs a token: set GITHUB_TOKEN")
        payload = json.dumps({"query": query, "variables": variables}).encode()
        body, _ = self.request(f"{API}/graphql", data=payload)
        errors = body.get("errors") or []
        # A field in an SSO-protected organisation (such as a fork's parent) comes
        # back null with a SAML error; the rest of the data is complete.
        saml_only = all((e.get("extensions") or {}).get("saml_failure") for e in errors)
        if errors and not (saml_only and body.get("data")):
            raise DataError(f"GraphQL errors: {errors}")
        self.sso_fallbacks += len(errors)
        return body["data"]


# --- collection ---

REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(first: 20, after: $cursor, ownerAffiliations: OWNER, privacy: PUBLIC) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name url description isFork isArchived stargazerCount createdAt pushedAt
        defaultBranchRef { name }
        parent { nameWithOwner defaultBranchRef { name } }
        repositoryTopics(first: 20) { nodes { topic { name } } }
        readmePlain: object(expression: "HEAD:README") {
          ... on Blob { text }
        }
        readmeMd: object(expression: "HEAD:README.md") {
          ... on Blob { text }
        }
        readmeRst: object(expression: "HEAD:README.rst") {
          ... on Blob { text }
        }
        readmeLower: object(expression: "HEAD:readme.md") {
          ... on Blob { text }
        }
        statusHidden: object(expression: "HEAD:.repostatus.org") {
          ... on Blob { text }
        }
        statusPlain: object(expression: "HEAD:repostatus.org") {
          ... on Blob { text }
        }
        codemeta: object(expression: "HEAD:codemeta.json") {
          ... on Blob { text }
        }
        citation: object(expression: "HEAD:CITATION.cff") {
          ... on Blob { text }
        }
      }
    }
  }
}
"""

PRIVATE_QUERY = """
query($login: String!) {
  user(login: $login) { contributionsCollection { restrictedContributionsCount } }
}
"""


def blob(node: dict[str, Any], field: str) -> str | None:
    return (node.get(field) or {}).get("text")


def fetch_repos(gh: GitHub, config: Config) -> list[Repo]:
    repos: list[Repo] = []
    cursor = None
    while True:
        data = gh.graphql(REPOS_QUERY, {"login": config.owner, "cursor": cursor})
        page = data["user"]["repositories"]
        for node in page["nodes"]:
            parent = node.get("parent") or {}
            repos.append(
                Repo(
                    name=node["name"],
                    url=node["url"],
                    description=node.get("description") or "",
                    fork=node["isFork"],
                    archived=node["isArchived"],
                    stars=node["stargazerCount"],
                    created=parse_date(node["createdAt"]),
                    pushed=parse_date(node["pushedAt"]),
                    topics=[
                        t["topic"]["name"] for t in node["repositoryTopics"]["nodes"]
                    ],
                    declared=declared_state(
                        [blob(node, f) for f in README_FIELDS],
                        [blob(node, "statusHidden"), blob(node, "statusPlain")],
                        blob(node, "codemeta"),
                    ),
                    paper_citation=citation_is_publication(blob(node, "citation")),
                    default_branch=(node.get("defaultBranchRef") or {}).get("name"),
                    parent=parent.get("nameWithOwner"),
                    parent_branch=(parent.get("defaultBranchRef") or {}).get("name"),
                    moved_to=config.moved.get(node["name"]),
                )
            )
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def repo_from_url(url: str) -> str:
    return url.split("/repos/", 1)[1]


def collect_own_events(
    gh: GitHub, repos: list[Repo], config: Config, today: dt.date
) -> None:
    since = today - dt.timedelta(days=YEAR_DAYS)
    by_name = {r.name: r for r in repos}
    owner = config.owner

    for repo in repos:
        if repo.archived or repo.pushed < since or not repo.default_branch:
            continue
        commits = gh.paginate(
            f"/repos/{owner}/{repo.name}/commits",
            {"since": f"{since.isoformat()}T00:00:00Z"},
        )
        for c in commits:
            # A fork's history holds upstream commits, including assistant-authored
            # ones from other people's sessions, so the assistant rule skips forks.
            if is_owner_commit(c, config, owned_repo=not repo.fork):
                repo.events.append(parse_date(c["commit"]["committer"]["date"]))

    def own_repo(item: dict[str, Any]) -> Repo | None:
        full = repo_from_url(item["repository_url"])
        o, _, name = full.partition("/")
        return by_name.get(name) if o == owner else None

    # Issue and PR comments by the owner.
    _, items = gh.search(
        "issues", f"commenter:{owner} user:{owner} updated:>={since.isoformat()}"
    )
    for item in items:
        repo = own_repo(item)
        if not repo:
            continue
        comments = gh.paginate(
            f"/repos/{owner}/{repo.name}/issues/{item['number']}/comments",
            {"since": f"{since.isoformat()}T00:00:00Z"},
        )
        for comment in comments:
            if (comment.get("user") or {}).get("login") == owner:
                repo.events.append(parse_date(comment["created_at"]))

    # Reviews by the owner on PRs in their repositories.
    _, items = gh.search(
        "issues",
        f"type:pr reviewed-by:{owner} user:{owner} updated:>={since.isoformat()}",
    )
    for item in items:
        repo = own_repo(item)
        if not repo:
            continue
        for review in gh.paginate(
            f"/repos/{owner}/{repo.name}/pulls/{item['number']}/reviews", {}
        ):
            when = review.get("submitted_at")
            if (
                (review.get("user") or {}).get("login") == owner
                and when
                and parse_date(when) >= since
            ):
                repo.events.append(parse_date(when))

    # Issues the owner closed.
    _, items = gh.search(
        "issues", f"type:issue user:{owner} is:closed closed:>={since.isoformat()}"
    )
    for item in items:
        repo = own_repo(item)
        if not repo:
            continue
        issue = gh.get(f"/repos/{owner}/{repo.name}/issues/{item['number']}")
        if (issue.get("closed_by") or {}).get("login") == owner and issue.get(
            "closed_at"
        ):
            repo.events.append(parse_date(issue["closed_at"]))


def collect_org_activity(
    gh: GitHub, config: Config, today: dt.date
) -> tuple[dict[str, OrgActivity], int]:
    owner = config.owner
    year_ago = today - dt.timedelta(days=YEAR_DAYS)
    count_since = today - dt.timedelta(days=ORG_COUNT_DAYS)
    orgs: dict[str, OrgActivity] = {}
    known_types: set[str] = set()

    def org_key(full: str) -> str:
        login = full.split("/", 1)[0]
        return config.org_aliases.get(login, login)

    def org_for(full: str) -> OrgActivity:
        key = org_key(full)
        return orgs.setdefault(key, OrgActivity(login=key))

    merged_total, merged = gh.search(
        "issues", f"type:pr is:merged author:{owner} -user:{owner}"
    )
    for item in merged:
        full = repo_from_url(item["repository_url"])
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        org = org_for(full)
        org.total += 1
        org.repos_total[full] += 1
        if merged_at:
            when = parse_date(merged_at)
            org.events.append(when)
            if when >= count_since:
                org.merged_recent += 1
                org.repos_recent[full] += 1

    _, reviewed = gh.search(
        "issues", f"type:pr reviewed-by:{owner} -author:{owner} -user:{owner}"
    )
    for item in reviewed:
        full = repo_from_url(item["repository_url"])
        org = org_for(full)
        org.total += 1
        org.repos_total[full] += 1
        if parse_date(item["updated_at"]) < count_since:
            continue  # A review cannot be newer than the PR's last update.
        dates = [
            parse_date(r["submitted_at"])
            for r in gh.paginate(f"/repos/{full}/pulls/{item['number']}/reviews", {})
            if (r.get("user") or {}).get("login") == owner and r.get("submitted_at")
        ]
        org.events.extend(dates)
        if any(d >= count_since for d in dates):
            org.reviewed_recent += 1
            org.repos_recent[full] += 1

    _, commits = gh.search(
        "commits", f"author:{owner} committer-date:>={year_ago.isoformat()}"
    )
    # Commits only add dates to organisations where the owner has merged PRs or
    # reviews. Commit search also finds the owner's commits in other people's
    # copies and forks of a repository, which must not become entries.
    seen: set[str] = set()
    for item in commits:
        repository = item.get("repository") or {}
        full = repository.get("full_name", "")
        if (
            not full
            or full.split("/", 1)[0] == owner
            or repository.get("private")
            or repository.get("fork")
            or org_key(full) not in orgs
            or item.get("sha") in seen
        ):
            continue
        seen.add(item.get("sha"))
        org = orgs[org_key(full)]
        if owner_type := (repository.get("owner") or {}).get("type"):
            org.owner_type = owner_type
            known_types.add(org.login)
        org.events.append(parse_date(item["commit"]["committer"]["date"]))

    # Issue search results do not say whether the owner is a user or an organisation.
    for org in orgs.values():
        if org.login not in known_types and org.login not in config.org_names:
            org.owner_type = gh.get(f"/users/{org.login}").get("type", "Organization")
    return orgs, merged_total


def fork_has_own_commits(gh: GitHub, repo: Repo, config: Config) -> bool | None:
    if not (repo.parent and repo.parent_branch and repo.default_branch):
        return None
    try:
        compare = gh.get(
            f"/repos/{repo.parent}/compare/{repo.parent_branch}...{config.owner}:{repo.default_branch}"
        )
    except DataError:
        return None
    return any(
        is_owner_commit(c, config, owned_repo=False) for c in compare.get("commits", [])
    )


# --- report ---


def build_report(
    gh: GitHub,
    repos: list[Repo],
    states: dict[str, list[Repo]],
    orgs: dict[str, OrgActivity],
    current: list[OrgActivity],
    past: list[OrgActivity],
    private_count: int,
    config: Config,
    today: dt.date,
) -> str:
    lines = [f"## Profile stats report ({today.isoformat()})", ""]
    lines.append(
        f"API calls: {gh.calls}. "
        f"SSO fallbacks to unauthenticated requests: {gh.sso_fallbacks}."
    )
    for query in gh.capped_searches:
        lines.append(f"Search capped at 1,000 results, counts may be low: `{query}`")
    lines.append("")
    lines.append("| State | Repositories |")
    lines.append("|---|---|")
    for state in sorted(states):
        names = ", ".join(sorted(r.name for r in states[state]))
        lines.append(f"| {state} | {names} |")
    flags = [(r.name, msg) for r in repos if (msg := contradiction(r, today))]
    lines += ["", "### Contradictions", ""]
    lines += [f"- {name}: {msg}" for name, msg in flags] or ["None."]

    unstated = [r for r in repos if classify(r, today) is None]
    lines += ["", "### Repositories without a state", ""]
    for repo in sorted(unstated, key=lambda r: r.pushed, reverse=True):
        if repo.fork:
            own = fork_has_own_commits(gh, repo, config)
            if own is False:
                continue
            tag = " (fork)" if own else " (fork, own commits unknown)"
        else:
            tag = ""
        last = (
            max(repo.events).isoformat()
            if repo.events
            else f"last push {repo.pushed.isoformat()}"
        )
        lines.append(f"- [{repo.name}]({repo.url}){tag}: {last}")

    lines += [
        "",
        "### Organisations",
        "",
        "| Organisation | Windows | Latest | 2-year PRs/reviews | All time "
        "| Shown as |",
    ]
    lines.append("|---|---|---|---|---|---|")
    current_logins = {o.login for o in current}
    past_logins = {o.login for o in past}
    for org in sorted(orgs.values(), key=lambda o: -o.total):
        shown = (
            "current"
            if org.login in current_logins
            else "earlier"
            if org.login in past_logins
            else "-"
        )
        latest = max(org.events).isoformat() if org.events else "-"
        lines.append(
            f"| {org.login} | {active_windows(org.events, today)}/{ORG_WINDOWS} "
            f"| {latest} | "
            f"{org.merged_recent}/{org.reviewed_recent} | {org.total} | {shown} |"
        )
    lines += ["", f"Private contributions in the last year: {private_count:,}."]
    return "\n".join(lines) + "\n"


# --- main ---


def run(config: Config, readme_path: Path, today: dt.date, dry_run: bool) -> int:
    gh = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    repos = fetch_repos(gh, config)
    if not repos:
        raise DataError("no repositories returned for the owner")
    collect_own_events(gh, repos, config, today)
    if not any(r.events for r in repos):
        raise DataError(
            "no owner events found in any repository; refusing to publish empty data"
        )
    orgs, merged_total = collect_org_activity(gh, config, today)
    if merged_total == 0:
        raise DataError(
            "merged pull request search returned nothing; "
            "refusing to publish empty data"
        )
    private_count = gh.graphql(PRIVATE_QUERY, {"login": config.owner})["user"][
        "contributionsCollection"
    ]["restrictedContributionsCount"]

    states: dict[str, list[Repo]] = defaultdict(list)
    for repo in repos:
        if state := classify(repo, today):
            states[state].append(repo)
    current = sorted(
        (o for o in orgs.values() if org_is_current(o, today)), key=lambda o: -o.total
    )
    past = sorted(
        (
            o
            for o in orgs.values()
            if o not in current and o.total >= PAST_MIN_CONTRIBUTIONS
        ),
        key=lambda o: (-o.total, o.login.lower()),
    )

    block = render_block(states, current, past, private_count, config, today)
    report = build_report(
        gh, repos, states, orgs, current, past, private_count, config, today
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                report + "\n### Generated block\n\n```markdown\n" + block + "\n```\n"
            )
    print(report)
    print(block)

    readme = readme_path.read_text(encoding="utf-8")
    updated = replace_block(readme, block)
    if dry_run:
        print("Dry run: README.md not written.")
    elif updated != readme:
        readme_path.write_text(updated, encoding="utf-8", newline="\n")
        print("README.md updated.")
    else:
        print("README.md unchanged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("profile.toml"))
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, default=dt.datetime.now(dt.UTC).date()
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the block without writing README.md",
    )
    args = parser.parse_args(argv)
    try:
        return run(Config.load(args.config), args.readme, args.today, args.dry_run)
    except DataError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
