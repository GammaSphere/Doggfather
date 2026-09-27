"""T2: role isolation is enforced by the backend, not painted on the UI.

These tests go through the real HTTP endpoints with each role's credentials,
the same way curl or the acceptance checker would.
"""

from datetime import timedelta

import pytest

from doggfather import audit, clock
from doggfather.db import fetch_value
from doggfather.services.events import get_event
from tests.helpers import login, post_form

SLUG = "sample-hack-2026"
JUDGE_A = "jdg_26"
JUDGE_B = "jdg_24"


# --------------------------------------------------------- score endpoints

def test_judge_reads_only_their_own_scores(as_role):
    response = as_role("judge_a").get("/api/judge/scores")
    assert response.status_code == 200
    body = response.json()
    assert body["judge"]["id"] == JUDGE_A
    assert body["count"] == 10  # jdg_26 filed ten fixture scorecards
    assert {s["project_id"] for s in body["scores"]} >= {"prj_02", "prj_41"}


def test_peer_scores_are_refused_by_query_and_by_path(as_role):
    judge_b = as_role("judge_b")
    for url in (f"/api/judge/scores?judge={JUDGE_A}", f"/api/judges/{JUDGE_A}/scores",
                "/api/judge/scores?judge=jonas.vogel@example.org"):
        response = judge_b.get(url)
        assert response.status_code == 403, url
        assert "scores" not in response.json()


def test_unknown_judge_ids_do_not_leak_existence(as_role):
    judge_b = as_role("judge_b")
    assert judge_b.get("/api/judges/jdg_99/scores").status_code == 403
    assert judge_b.get("/api/judge/scores?judge=judge_a").status_code == 403


@pytest.mark.parametrize("role,expected", [
    (None, 401), ("participant", 403), ("judge_b", 403), ("judge_a", 200), ("organizer", 200),
])
def test_role_matrix_for_one_judges_scores(as_role, role, expected):
    assert as_role(role).get(f"/api/judges/{JUDGE_A}/scores").status_code == expected


def test_participant_is_not_a_judge(as_role):
    response = as_role("participant").get("/api/judge/scores")
    assert response.status_code == 403
    assert response.json()["error"] == "not_a_judge"


def test_visitor_is_asked_to_authenticate(as_role):
    assert as_role(None).get("/api/judge/scores").status_code == 401


def test_admin_can_read_any_judge(seeded_app, as_role):
    from fastapi.testclient import TestClient

    admin = TestClient(seeded_app)
    admin.__enter__()
    assert login(admin, "admin@doggfather.local", "dogfood-demo-2026").status_code == 303
    assert admin.get(f"/api/judges/{JUDGE_B}/scores").status_code == 200
    admin.__exit__(None, None, None)


# ------------------------------------------------------------ judge pages

def pending_for(conn, judge_id):
    return fetch_value(conn, "SELECT project_id FROM assignments WHERE judge_id = ? AND status = 'pending'", (judge_id,))


def test_judge_cannot_open_projects_outside_their_queue(as_role, conn):
    judge_a = as_role("judge_a")
    # prj_01 sits in trk_04 (Security); jdg_26 covers trk_01 and trk_03 and was never assigned it.
    assert judge_a.get(f"/judge/{SLUG}/projects/prj_01").status_code == 403
    response = post_form(judge_a, f"/judge/{SLUG}/projects/prj_01", {"c_evt_01:functionality": "5"},
                         token_from=f"/judge/{SLUG}")
    assert response.status_code == 403
    assert "not in your judging queue" in response.text  # refused by isolation, not by CSRF


def test_judge_scores_an_assigned_project_and_moves_on(as_role, conn):
    judge_a = as_role("judge_a")
    project_id = pending_for(conn, JUDGE_A)
    page = f"/judge/{SLUG}/projects/{project_id}"
    html = judge_a.get(page).text
    assert "Your scorecard" in html and "Diego" not in html  # nothing about peer judges
    response = post_form(judge_a, page, {"c_evt_01:functionality": "4", "c_evt_01:quality": "3",
                                         "c_evt_01:innovation": "5", "comment": "Tight scope."}, token_from=page)
    assert response.status_code == 303
    assert fetch_value(conn, "SELECT status FROM assignments WHERE judge_id = ? AND project_id = ?",
                       (JUDGE_A, project_id)) == "done"
    assert audit.entries(conn, action_prefix="score.submit", limit=1)[0]["actor_id"] == JUDGE_A
    assert as_role("judge_a").get("/api/judge/scores").json()["count"] == 11


def test_incomplete_or_out_of_range_scorecards_are_rejected(as_role, conn):
    judge_a = as_role("judge_a")
    project_id = pending_for(conn, JUDGE_A)
    page = f"/judge/{SLUG}/projects/{project_id}"
    response = post_form(judge_a, page, {"c_evt_01:functionality": "9", "c_evt_01:quality": "3"}, token_from=page)
    assert response.status_code == 422
    assert fetch_value(conn, "SELECT COUNT(*) FROM scores WHERE judge_id = ? AND project_id = ?", (JUDGE_A, project_id)) == 0


def test_no_scoring_after_judging_closes(as_role, conn):
    judge_a = as_role("judge_a")
    project_id = pending_for(conn, JUDGE_A)
    page = f"/judge/{SLUG}/projects/{project_id}"
    token = judge_a.get(page).text.split('name="csrf_token" value="')[1].split('"')[0]
    closes = clock.parse(get_event(conn, "evt_01").judging_close_at)
    with clock.frozen(closes + timedelta(minutes=1)):
        response = judge_a.post(page, data={"csrf_token": token, "c_evt_01:functionality": "4",
                                            "c_evt_01:quality": "3", "c_evt_01:innovation": "5"}, follow_redirects=False)
    assert response.status_code == 403


def test_participants_and_visitors_have_no_judge_pages(as_role):
    assert as_role("participant").get(f"/judge/{SLUG}").status_code == 403
    assert as_role(None).get(f"/judge/{SLUG}", follow_redirects=False).status_code == 303  # to login
