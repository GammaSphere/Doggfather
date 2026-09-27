"""Pairwise bonus: comparison judging with active pair selection and Bradley-Terry."""

import pytest

from doggfather import auth
from doggfather.db import fetch_value
from doggfather.errors import Conflict, Forbidden
from doggfather.services import comparisons
from doggfather.services.events import get_event
from tests.helpers import post_form

SLUG = "sample-hack-2026"


@pytest.fixture
def pairwise_on(conn):
    conn.execute("UPDATE events SET pairwise_enabled = 1 WHERE id = 'evt_01'")
    return get_event(conn, "evt_01")


@pytest.fixture
def judge_a(conn):
    return auth.get_user(conn, "jdg_26")


def test_pairs_come_from_the_judges_own_pool(conn, pairwise_on, judge_a):
    pool = set(comparisons.pool(conn, judge_a.id, pairwise_on))
    # jdg_26 covers Developer tools (6) + Accessibility (6) projects.
    assert len(pool) == 12
    a, b = comparisons.next_pair(conn, judge_a, pairwise_on)
    assert a != b and {a, b} <= pool


def test_every_pair_once_then_done(conn, pairwise_on):
    judge = auth.get_user(conn, "jdg_01")  # Accessibility only: 6 projects, 15 pairs
    seen = set()
    while (pair := comparisons.next_pair(conn, judge, pairwise_on)) is not None:
        assert frozenset(pair) not in seen
        seen.add(frozenset(pair))
        comparisons.record(conn, judge, pairwise_on, pair[0], pair[1])
    assert len(seen) == 15
    assert comparisons.judge_progress(conn, judge.id, pairwise_on) == {"done": 15, "possible": 15}


def test_repeats_and_out_of_pool_projects_are_refused(conn, pairwise_on, judge_a):
    a, b = comparisons.next_pair(conn, judge_a, pairwise_on)
    comparisons.record(conn, judge_a, pairwise_on, a, b)
    with pytest.raises(Conflict):
        comparisons.record(conn, judge_a, pairwise_on, b, a)
    with pytest.raises(Forbidden):
        comparisons.record(conn, judge_a, pairwise_on, "prj_01", a)  # Security track: not jdg_26's


def test_pairwise_must_be_switched_on(conn, judge_a):
    with pytest.raises(Forbidden):
        comparisons.next_pair(conn, judge_a, get_event(conn, "evt_01"))


def test_consistent_preferences_produce_the_expected_ranking(conn, pairwise_on, as_role):
    judge = auth.get_user(conn, "jdg_01")
    order = sorted(comparisons.pool(conn, judge.id, pairwise_on))  # prefers lower ids, always
    while (pair := comparisons.next_pair(conn, judge, pairwise_on)) is not None:
        winner, loser = sorted(pair)
        comparisons.record(conn, judge, pairwise_on, winner, loser)
    ranking = as_role("organizer").get("/api/v1/events/evt_01/pairwise").json()
    compared = [r["id"] for r in ranking if r["comparisons"]]
    assert compared == order


def test_direct_verdicts_move_the_bt_results_method(conn, pairwise_on, as_role):
    org = as_role("organizer")
    before = {r["id"]: r["pairwise_rank"] for r in org.get("/api/v1/events/evt_01/results").json()["results"]}
    judge = auth.get_user(conn, "jdg_01")
    while (pair := comparisons.next_pair(conn, judge, pairwise_on)) is not None:
        loser = "prj_03" if "prj_03" in pair else pair[1]
        winner = pair[0] if pair[1] == loser else pair[1]
        comparisons.record(conn, judge, pairwise_on, winner, loser)
    after = {r["id"]: r["pairwise_rank"] for r in org.get("/api/v1/events/evt_01/results").json()["results"]}
    assert after["prj_03"] > before["prj_03"]  # lost every direct duel, so it drops


def test_compare_page_flow(as_role, pairwise_on, conn):
    judge = as_role("judge_a")
    page = f"/judge/{SLUG}/compare"
    html = judge.get(page).text
    assert "Which is" in html and 'data-side="A"' in html
    winner = html.split('name="winner" value="')[1].split('"')[0]
    loser = html.split('name="loser" value="')[1].split('"')[0]
    assert post_form(judge, page, {"winner": winner, "loser": loser}, token_from=page).status_code == 303
    assert fetch_value(conn, "SELECT COUNT(*) FROM pairwise_comparisons WHERE judge_id = 'jdg_26'") == 1
    assert "Pairwise mode (1 compared)" in judge.get(f"/judge/{SLUG}").text


def test_pairwise_api(as_role, pairwise_on, seeded_app):
    judge = as_role("judge_b")
    nxt = judge.get("/api/v1/judge/pairs/next", params={"event": "evt_01"}).json()
    winner, loser = nxt["pair"]
    created = judge.post("/api/v1/judge/pairs", params={"event": "evt_01"}, json={"winner_id": winner, "loser_id": loser})
    assert created.status_code == 201
    assert as_role("judge_b").get("/api/v1/events/evt_01/pairwise").status_code == 403  # rankings are organizer-only
