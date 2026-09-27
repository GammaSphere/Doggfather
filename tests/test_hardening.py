"""Regression tests for the independent security review.

Each test reproduces a reported weakness; all of them failed before the fix.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from doggfather import auth, clock
from doggfather.db import fetch_value
from doggfather.services import duplicates, judges
from doggfather.services import events as event_service
from doggfather.services.events import get_event
from tests.factories import new_user_client, open_event
from tests.helpers import login, post_form


def mallory_event(conn):
    """An organizer of an unrelated event, who also seats judge A there."""
    mallory = auth.ensure_user(conn, "mallory@example.org", "Mallory", password="mallory-password-1")
    now = clock.now()
    event = event_service.create_event(conn, mallory, {
        "name": "Evil Hack", "slug": "evil-hack",
        "submissions_open_at": clock.iso(now - timedelta(days=1)),
        "submissions_close_at": clock.iso(now + timedelta(days=1)),
        "judging_close_at": clock.iso(now + timedelta(days=5)),
    })
    event_service.add_track(conn, mallory, event, "Anything")
    return mallory, event_service.get_event(conn, event.id)


def client_for(app, email, password):
    client = TestClient(app)
    client.__enter__()
    assert login(client, email, password).status_code == 303
    return client


# ---- 1. cross-event score disclosure -------------------------------------

def test_organizer_of_another_event_cannot_read_a_shared_judges_scores(seeded_app, conn):
    mallory, event = mallory_event(conn)
    judges.add_judge(conn, event, auth.get_user(conn, "jdg_26"), [])  # jdg_26 legitimately judges both events
    client = client_for(seeded_app, "mallory@example.org", "mallory-password-1")
    for url in ("/api/judge/scores?judge=jdg_26", "/api/judges/jdg_26/scores"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["count"] == 0  # only Evil Hack's scores, and there are none
    assert client.get("/api/judges/jdg_26/scores?event=evt_01").status_code == 403


def test_event_parameter_is_not_an_oracle_for_judge_membership(as_role):
    assert as_role("organizer").get("/api/judges/jdg_26/scores?event=evt_nope").status_code in (403, 404)


def test_only_admins_import_bundles(seeded_app, conn):
    mallory_event(conn)
    client = client_for(seeded_app, "mallory@example.org", "mallory-password-1")
    bundle = {"event": {"id": "evt_evil2", "name": "Evil 2", "submissions_close": "2027-01-01T00:00:00Z"},
              "judges": [{"id": "jx", "email": "jonas.vogel@example.org"}]}
    assert client.post("/api/v1/bundles", json=bundle).status_code == 403
    assert fetch_value(conn, "SELECT COUNT(*) FROM events WHERE id = 'evt_evil2'") == 0


# ---- 2. cross-event rubric weights --------------------------------------

def test_rubric_overrides_cannot_touch_another_events_criteria(seeded_app, conn, as_role):
    _, event = mallory_event(conn)
    client = client_for(seeded_app, "mallory@example.org", "mallory-password-1")
    before = [r["id"] for r in as_role("organizer").get("/api/v1/events/evt_01/results").json()["results"]]
    criteria = client.get(f"/api/v1/events/{event.id}/rubric").json()["criteria"]
    attack = {"criteria": criteria,
              "track_weights": {"trk_01": {"evt_01:functionality": 0, "evt_01:innovation": 100}}}
    assert client.put(f"/api/v1/events/{event.id}/rubric", json=attack).status_code == 422
    assert fetch_value(conn, "SELECT COUNT(*) FROM criterion_track_weights") == 0
    after = [r["id"] for r in as_role("organizer").get("/api/v1/events/evt_01/results").json()["results"]]
    assert before == after


def test_schema_refuses_a_track_weight_across_events(conn):
    import sqlite3

    _, event = mallory_event(conn)
    track = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ?", (event.id,))
    with pytest.raises(sqlite3.IntegrityError, match="same event"):
        conn.execute("INSERT INTO criterion_track_weights VALUES ('evt_01:quality', ?, 3)", (track,))


# ---- 3. vote code attempt counter ---------------------------------------

def test_wrong_codes_are_counted_and_lock_the_code(seeded_app, conn):
    client = TestClient(seeded_app)
    client.__enter__()
    page = "/vote/sample-hack-2026"
    client.get(page)
    post_form(client, "/vote/sample-hack-2026/code", {"email": "guesser@example.org"}, token_from=page)
    body = fetch_value(conn, "SELECT body FROM outbox WHERE to_email = 'guesser@example.org'")
    real = body.split("Your code is ")[1][:6]
    for _ in range(5):
        post_form(client, "/vote/sample-hack-2026/verify", {"code": "000000" if real != "000000" else "111111"}, token_from=page)
    assert fetch_value(conn, "SELECT attempts FROM vote_codes") == 5
    post_form(client, "/vote/sample-hack-2026/verify", {"code": real}, token_from=page)
    assert fetch_value(conn, "SELECT COUNT(*) FROM voters") == 0  # locked after five wrong guesses


# ---- 4. own-project detection with address aliases ----------------------

def test_aliased_team_member_email_still_counts_as_own_project(as_role, conn):
    conn.execute("UPDATE users SET email = 'priya.one+hack@gmail.com' WHERE email = 'priya1@example.org'")
    priya = as_role("participant")
    html = priya.get("/vote/sample-hack-2026").text
    assert "your team" in html and 'name="v_prj_01"' not in html
    response = post_form(priya, "/vote/sample-hack-2026/ballot", {"v_prj_01": "3"}, token_from="/vote/sample-hack-2026")
    assert "own team" in priya.get(response.headers["location"]).text
    assert fetch_value(conn, "SELECT user_id FROM voters") is not None


# ---- 5. withdrawn projects stay withdrawn -------------------------------

@pytest.fixture
def withdrawn_project(seeded_app, conn):
    event = open_event(conn)
    builder = new_user_client(seeded_app, "builder2@example.org")
    page = f"/events/{event.slug}/team"
    post_form(builder, page, {"team_name": "Resurrectors"}, token_from=page)
    track = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ? LIMIT 1", (event.id,))
    project_id = builder.post("/projects/new", json={
        "event": event.id, "title": "Phoenix", "tagline": "t", "track_id": track, "repo_url": "https://example.org/p",
        "description": "A project long enough to be submitted in the first place."}).json()["id"]
    builder.post(f"/projects/{project_id}/submit", json={})
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    duplicates.withdraw(conn, organizer, get_event(conn, event.id), project_id, "ineligible")
    return builder, project_id


def test_team_cannot_resubmit_a_withdrawn_project(withdrawn_project, conn):
    builder, project_id = withdrawn_project
    assert builder.post(f"/api/v1/projects/{project_id}/submit").status_code == 403
    assert builder.post(f"/projects/{project_id}/edit", json={"title": "Phoenix 2"}).status_code == 403
    assert builder.post(f"/projects/{project_id}/unsubmit", json={}).status_code == 403
    assert fetch_value(conn, "SELECT status FROM projects WHERE id = ?", (project_id,)) == "withdrawn"


def test_last_member_cannot_delete_a_withdrawn_projects_record(withdrawn_project, conn):
    builder, project_id = withdrawn_project
    team_id = fetch_value(conn, "SELECT team_id FROM projects WHERE id = ?", (project_id,))
    builder.post(f"/api/v1/teams/{team_id}/leave")
    assert fetch_value(conn, "SELECT COUNT(*) FROM projects WHERE id = ?", (project_id,)) == 1


# ---- 6. published results are frozen ------------------------------------

def test_published_results_cannot_be_reweighted_or_trimmed(as_role, conn):
    conn.execute("UPDATE events SET judging_close_at = '2026-03-05T00:00:00Z', results_published_at = '2026-03-06T00:00:00Z'")
    org = as_role("organizer")
    rubric = org.get("/api/v1/events/evt_01/rubric").json()
    criteria = [{**c, "weight": 9 if c["key"] == "quality" else c["weight"]} for c in rubric["criteria"]]
    assert org.put("/api/v1/events/evt_01/rubric", json={"criteria": criteria}).status_code == 409
    assert org.post("/api/v1/events/evt_01/projects/prj_05/withdraw", json={"reason": "x"}).status_code == 409


# ---- 7. hostile inputs are 4xx, never 500 --------------------------------

def test_edge_inputs_do_not_crash(as_role):
    visitor = as_role(None)
    assert visitor.get("/projects?page=100000000000000000000").status_code == 200
    org = as_role("organizer")
    assert post_form(org, "/organize/sample-hack-2026/rubric", {"rows": "abc"},
                     token_from="/organize/sample-hack-2026/rubric").status_code < 500
    assert post_form(org, "/organize/sample-hack-2026/assignments/auto", {"target": "lots"},
                     token_from="/organize/sample-hack-2026/assignments").status_code < 500


def test_bad_bundles_are_client_errors(seeded_app, conn):
    admin = client_for(seeded_app, "admin@doggfather.local", "dogfood-demo-2026")
    bad_number = {"event": {"id": "evt_n", "name": "N", "submissions_close": "2027-01-01T00:00:00Z", "max_team_size": "many"}}
    assert admin.post("/api/v1/bundles", json=bad_number).status_code == 422
    collision = {"event": {"id": "evt_c", "name": "C", "submissions_close": "2027-01-01T00:00:00Z"},
                 "tracks": [{"id": "trk_01", "name": "Already taken elsewhere"}]}
    assert admin.post("/api/v1/bundles", json=collision).status_code == 409
    assert fetch_value(conn, "SELECT COUNT(*) FROM events WHERE id IN ('evt_n', 'evt_c')") == 0
