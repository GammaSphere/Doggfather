"""T1: public gallery with search and multi-select filters."""

import re

from tests.conftest import FIXTURES

CARD_RE = re.compile(r'href="/projects/(prj_[0-9a-z]+)"')


def ids(html: str) -> set[str]:
    return set(CARD_RE.findall(html))


def test_gallery_is_public_and_shows_fixture_projects(as_role):
    response = as_role(None).get("/projects")
    assert response.status_code == 200
    for project in FIXTURES["projects"][:3]:  # exactly what run.py looks for
        assert project["title"] in response.text
    assert len(ids(response.text)) == 41


def test_search_matches_title_team_and_is_case_insensitive(as_role):
    visitor = as_role(None)
    # prj_20 matches through its team, HollowHarbour.
    assert ids(visitor.get("/projects?q=HARBOUR").text) == {"prj_07", "prj_41", "prj_30", "prj_20"}
    assert ids(visitor.get("/projects?q=northkiln").text) == {"prj_01"}  # team name
    assert ids(visitor.get("/projects?q=dry+harbour").text) == {"prj_07", "prj_41"}  # all tokens must match


def test_like_wildcards_are_literal(as_role):
    response = as_role(None).get("/projects?q=%25")
    assert ids(response.text) == set()
    assert "Nothing matches" in response.text


def test_multi_track_filter_is_or(as_role):
    html = as_role(None).get("/projects?track=trk_01&track=trk_08").text
    expected = {p["id"] for p in FIXTURES["projects"] if p["track"] in ("trk_01", "trk_08")}
    assert ids(html) == expected and len(expected) == 12


def test_multi_tag_filter_is_and(as_role, conn):
    conn.executemany("INSERT INTO project_tags VALUES (?, ?)",
                     [("prj_01", "rust"), ("prj_01", "wasm"), ("prj_02", "rust"), ("prj_03", "wasm")])
    visitor = as_role(None)
    assert ids(visitor.get("/projects?tag=rust").text) == {"prj_01", "prj_02"}
    assert ids(visitor.get("/projects?tag=rust&tag=wasm").text) == {"prj_01"}
    assert "#wasm" in visitor.get("/projects").text  # facet listed


def test_drafts_never_appear(as_role, conn):
    conn.execute("UPDATE projects SET status = 'draft' WHERE id = 'prj_02'")
    html = as_role(None).get("/projects").text
    assert "prj_02" not in ids(html) and len(ids(html)) == 40


def test_pagination(as_role):
    visitor = as_role(None)
    first = ids(visitor.get("/projects?per=24&sort=title").text)
    second = ids(visitor.get("/projects?per=24&sort=title&page=2").text)
    assert len(first) == 24 and len(second) == 17 and not first & second


def test_bad_parameters_do_not_break_the_page(as_role):
    assert as_role(None).get("/projects?sort=DROP+TABLE&per=100000&page=-4").status_code == 200
