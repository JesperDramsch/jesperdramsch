import datetime as dt
from collections import Counter

import profile_stats as ps
import pytest

TODAY = dt.date(2026, 10, 12)

CONFIG = ps.Config(
    owner="JesperDramsch",
    projects_url="https://dramsch.net/projects",
    max_entries=2,
    names=["Jesper Dramsch", "JesperDramsch"],
    assistant_emails=["noreply@anthropic.com"],
    org_names={"ecmwf": "ECMWF"},
    moved={},
)


def days_ago(*ages: int) -> list[dt.date]:
    return [TODAY - dt.timedelta(days=a) for a in ages]


def make_repo(**overrides) -> ps.Repo:
    fields = dict(
        name="repo",
        url="https://github.com/JesperDramsch/repo",
        description="A repo.",
        fork=False,
        archived=False,
        stars=0,
        created=dt.date(2020, 1, 1),
        pushed=dt.date(2020, 1, 1),
        topics=[],
        declared=None,
        paper_citation=False,
        default_branch="main",
    )
    fields.update(overrides)
    return ps.Repo(**fields)


def commit(login=None, name="Someone", email="x@example.com", message="msg"):
    return {
        "author": {"login": login} if login else None,
        "commit": {"author": {"name": name, "email": email}, "message": message},
    }


# --------------------------------------------------------------- declared states

BADGE = "[![s](https://www.repostatus.org/badges/latest/{}.svg)](https://www.repostatus.org/#{})"


def test_badge_in_readme_is_read():
    assert ps.state_from_text(BADGE.format("active", "active")) == "active"


def test_unknown_badge_state_is_ignored():
    assert ps.state_from_text(BADGE.format("maintenance", "maintenance")) is None


def test_readme_badge_wins_over_status_file_and_codemeta():
    state = ps.declared_state(
        [None, BADGE.format("wip", "wip")],
        ["https://www.repostatus.org/badges/latest/inactive.svg"],
        '{"developmentStatus": "active"}',
    )
    assert state == "wip"


def test_hidden_status_file_is_read():
    files = ["https://www.repostatus.org/badges/latest/inactive.svg\n"]
    assert ps.declared_state([None], files, None) == "inactive"


@pytest.mark.parametrize(
    "value", ["inactive", "https://www.repostatus.org/#inactive", "Inactive"]
)
def test_codemeta_status_accepts_ids_and_uris(value):
    assert ps.state_from_codemeta(f'{{"developmentStatus": "{value}"}}') == "inactive"


def test_codemeta_custom_value_is_ignored():
    assert ps.state_from_codemeta('{"developmentStatus": "maintenance"}') is None


def test_broken_codemeta_is_ignored():
    assert ps.state_from_codemeta("{not json") is None


# --------------------------------------------------------------- citation

CFF_CONFERENCE = """cff-version: '1.2.0'
type: 'book'
preferred-citation:
  doi: '10.1190/segam2018-2996783.1'
  title: 'Deep-learning seismic facies'
"""

CFF_SOFTWARE_WITH_NESTED_ARTICLE = """cff-version: '1.2.0'
type: 'software'
preferred-citation:
  type: 'software'
  title: 'keras-complex'
  references:
    - title: 'Deep Complex Networks'
      type: 'article'
"""


def test_preferred_citation_with_doi_and_no_type_is_publication():
    assert ps.citation_is_publication(CFF_CONFERENCE)


def test_book_chapter_type_is_publication():
    cff = "preferred-citation:\n  type: 'book-chapter'\n  title: 'Chapter One'\n"
    assert ps.citation_is_publication(cff)


def test_nested_reference_type_does_not_leak():
    assert not ps.citation_is_publication(CFF_SOFTWARE_WITH_NESTED_ARTICLE)


def test_no_preferred_citation_is_not_publication():
    assert not ps.citation_is_publication("cff-version: 1.2.0\ntitle: x\n")
    assert not ps.citation_is_publication(None)


# --------------------------------------------------------------- commit attribution


def test_owner_login_counts():
    assert ps.is_owner_commit(commit(login="JesperDramsch"), CONFIG, owned_repo=False)


def test_owner_name_counts_without_linked_login():
    assert ps.is_owner_commit(commit(name="Jesper Dramsch"), CONFIG, owned_repo=False)


def test_co_author_trailer_counts():
    message = "fix\n\nCo-authored-by: Jesper Dramsch <jesper@example.com>"
    assert ps.is_owner_commit(commit(message=message), CONFIG, owned_repo=False)


def test_assistant_commit_counts_only_in_owned_repo():
    c = commit(name="Claude", email="noreply@anthropic.com")
    assert ps.is_owner_commit(c, CONFIG, owned_repo=True)
    assert not ps.is_owner_commit(c, CONFIG, owned_repo=False)


def test_bots_never_count():
    c = commit(login="claude[bot]", name="claude[bot]", email="noreply@anthropic.com")
    assert not ps.is_owner_commit(c, CONFIG, owned_repo=True)
    assert not ps.is_owner_commit(
        commit(login="dependabot[bot]"), CONFIG, owned_repo=True
    )


def test_stranger_does_not_count():
    assert not ps.is_owner_commit(commit(login="someone"), CONFIG, owned_repo=True)


# --------------------------------------------------------------- classification


def test_archived_is_unsupported_even_when_declared():
    repo = make_repo(archived=True, declared="inactive")
    assert ps.classify(repo, TODAY) == "unsupported"


def test_archived_with_successor_is_moved():
    repo = make_repo(archived=True, moved_to="https://github.com/ecmwf/anemoi-core")
    assert ps.classify(repo, TODAY) == "moved"


def test_declared_state_wins_over_activity():
    repo = make_repo(declared="inactive", events=days_ago(*range(20)))
    assert ps.classify(repo, TODAY) == "inactive"


def test_active_needs_ten_events_in_a_year_and_one_recent():
    assert (
        ps.classify(make_repo(events=days_ago(1, *range(100, 109))), TODAY) == "active"
    )
    assert ps.classify(make_repo(events=days_ago(1, *range(100, 108))), TODAY) is None


def test_burst_without_recent_event_is_not_active():
    assert ps.classify(make_repo(events=days_ago(*range(100, 140))), TODAY) is None


def test_young_repo_with_recent_activity_is_wip():
    repo = make_repo(created=TODAY - dt.timedelta(days=83), events=days_ago(3, 10))
    assert ps.classify(repo, TODAY) == "wip"


def test_old_repo_with_light_activity_has_no_state():
    assert ps.classify(make_repo(events=days_ago(3, 10)), TODAY) is None


def test_contradiction_flags():
    busy = make_repo(declared="inactive", events=days_ago(*range(12)))
    assert "Inactive" in ps.contradiction(busy, TODAY)
    idle = make_repo(declared="active")
    assert "Active" in ps.contradiction(idle, TODAY)
    assert ps.contradiction(make_repo(declared="inactive"), TODAY) is None


# --------------------------------------------------------------- organisations


def test_org_current_needs_three_of_four_windows_and_recent_event():
    org = ps.OrgActivity(login="ecmwf", events=days_ago(30, 120, 200))
    assert ps.org_is_current(org, TODAY)


def test_org_with_two_windows_is_not_current():
    org = ps.OrgActivity(login="ecmwf", events=days_ago(30, 40, 120))
    assert not ps.org_is_current(org, TODAY)


def test_org_with_stale_latest_event_is_not_current():
    org = ps.OrgActivity(login="ecmwf", events=days_ago(190, 200, 300, 350))
    assert not ps.org_is_current(org, TODAY)


def test_active_windows_ignores_old_events():
    assert ps.active_windows(days_ago(10, 400, 800), TODAY) == 1


# --------------------------------------------------------------- notes and rendering


def test_note_priority_paper_then_workshop_then_teaching():
    assert ps.note_for(make_repo(paper_citation=True, topics=["workshop"])) == (
        "paper code"
    )
    assert ps.note_for(make_repo(topics=["tutorial", "workshop"])) == (
        "workshop material"
    )
    assert ps.note_for(make_repo(topics=["course"])) == "teaching"
    assert ps.note_for(make_repo()) is None


def test_round_down():
    assert ps.round_down(1467) == 1460
    assert ps.round_down(9) == 0


def render(states, current=(), past=(), private=0):
    return ps.render_block(states, list(current), list(past), private, CONFIG, TODAY)


def test_empty_sections_are_omitted_and_markers_wrap_block():
    block = render({"active": [make_repo(name="a", events=days_ago(1) * 12)]})
    assert block.startswith(ps.START_MARKER) and block.endswith(ps.END_MARKER)
    assert "**Actively developed**" in block
    assert "In progress" not in block
    assert "12 contributions in the last year" in block


def test_cap_adds_more_link_and_sorts_by_stars():
    repos = [make_repo(name=f"r{i}", stars=i, declared="inactive") for i in range(4)]
    block = render({"inactive": repos})
    assert block.index("[r3]") < block.index("[r2]")
    assert "[r1]" not in block
    assert (
        "- and 2 more at [dramsch.net/projects](https://dramsch.net/projects)" in block
    )


def test_notes_only_in_finished_section():
    paper = make_repo(name="p", paper_citation=True, declared="inactive", stars=5)
    lib = make_repo(name="lib", paper_citation=True, events=days_ago(1) * 12)
    block = render({"inactive": [paper], "active": [lib]})
    assert (
        "[p](https://github.com/JesperDramsch/repo): A repo (paper code), 5 stars"
        in (block)
    )
    assert "A repo, 12 contributions" in block


def test_singular_forms():
    block = render({"unsupported": [make_repo(stars=1, archived=True)]})
    assert "1 star" in block and "1 stars" not in block


def test_contributions_section():
    current = ps.OrgActivity(
        login="ecmwf",
        events=days_ago(30, 120, 200),
        merged_recent=28,
        reviewed_recent=31,
        total=126,
        repos_recent=Counter({"ecmwf/anemoi-core": 16}),
    )
    past = [
        ps.OrgActivity(login="softwareunderground", total=32),
        ps.OrgActivity(
            login="Elijas",
            owner_type="User",
            total=12,
            repos_total=Counter({"Elijas/auto-playlist": 12}),
        ),
    ]
    block = render({}, [current], past, private=1467)
    assert (
        "- Currently contributing to [ECMWF](https://github.com/ecmwf): "
        "28 merged pull requests and 31 reviews in the last two years, "
        "most in [anemoi-core](https://github.com/ecmwf/anemoi-core)"
    ) in block
    assert (
        "- Earlier: [softwareunderground](https://github.com/softwareunderground) (32)"
        " and [auto-playlist](https://github.com/Elijas/auto-playlist) (12)"
        " merged pull requests and reviews"
    ) in block
    assert "about 1,460 contributions to private repositories" in block


def test_tiny_private_count_is_omitted():
    assert "private" not in render({}, private=7)


@pytest.mark.parametrize(
    "query", ["author:x -user:x", "commenter:x user:x is:closed", "a is:public"]
)
def test_every_search_is_public_only(query):
    result = ps.GitHub.public_query(query)
    assert result.split().count("is:public") == 1
    assert result.startswith(query.replace(" is:public", ""))


def test_replace_block_swaps_only_marked_text():
    readme = f"intro\n{ps.START_MARKER}\nold\n{ps.END_MARKER}\noutro\n"
    new = f"{ps.START_MARKER}\nnew\n{ps.END_MARKER}"
    assert ps.replace_block(readme, new) == f"intro\n{new}\noutro\n"


def test_replace_block_requires_markers():
    with pytest.raises(ps.DataError):
        ps.replace_block("no markers here", "x")
