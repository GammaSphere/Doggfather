"""T1: draft, edit, submit; the deadline holds on every write path."""

from datetime import timedelta

import pytest

from doggfather import audit, clock
from doggfather.db import fetch_one, fetch_value
from tests.factories import new_user_client, open_event
from tests.helpers import post_form

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ------------------------------------------------------ the checker contract

def test_closed_event_refuses_json_submission_from_participant(as_role, conn):
    response = as_role("participant").post("/projects/new", json={"title": "dogfood-late-submission-probe", "summary": "probe"})
    assert response.status_code == 403
    assert response.json()["error"] == "submissions_closed"
    assert response.json()["closed_at"] == "2026-03-01T18:00:00Z"
    assert fetch_value(conn, "SELECT COUNT(*) FROM projects WHERE title = 'dogfood-late-submission-probe'") == 0
    refused = audit.entries(conn, action_prefix="project.refused", limit=1)[0]
    assert "dogfood-late-submission-probe" in refused["sentence"]


def test_submission_requires_login(as_role):
    response = as_role(None).post("/projects/new", json={"title": "x"})
    assert response.status_code == 401


def test_submission_requires_a_team(seeded_app, conn):
    open_event(conn)
    loner = new_user_client(seeded_app, "loner@example.org")
    response = loner.post("/projects/new", json={"title": "Solo thing", "event": "open-hack"})
    assert response.status_code == 403
    assert response.json()["error"] == "no_team"


# ---------------------------------------------------------- the happy path

@pytest.fixture
def team_client(seeded_app, conn):
    event = open_event(conn)
    client = new_user_client(seeded_app, "builder@example.org", "Bea Builder")
    page = f"/events/{event.slug}/team"
    post_form(client, page, {"team_name": "Builders"}, token_from=page)
    return client, event


def test_draft_edit_submit_lifecycle(team_client, as_role, conn):
    client, event = team_client
    track = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ? AND name = 'General'", (event.id,))
    response = post_form(client, "/projects/new", {"event": event.id, "title": "Lantern"}, token_from=f"/projects/new?event={event.slug}")
    assert response.status_code == 303
    project_id = response.headers["location"].split("/")[2]

    # Drafts are private.
    assert as_role(None).get(f"/projects/{project_id}").status_code == 404
    assert client.get(f"/projects/{project_id}").status_code == 200

    # Submitting an incomplete draft lists what is missing.
    edit = f"/projects/{project_id}/edit"
    incomplete = post_form(client, edit, {"title": "Lantern", "action": "submit"}, token_from=edit)
    assert incomplete.status_code == 422
    assert fetch_value(conn, "SELECT status FROM projects WHERE id = ?", (project_id,)) == "draft"

    complete = post_form(client, edit, {
        "title": "Lantern", "tagline": "Lights up dark dashboards", "track_id": track,
        "description": "Lantern watches your dashboards and tells you when they go dark.",
        "repo_url": "https://example.org/lantern", "tags": "python, sqlite  fastapi", "action": "submit",
    }, token_from=edit)
    assert complete.status_code == 303
    row = fetch_one(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
    assert row["status"] == "submitted" and row["submitted_at"]
    tags = [r[0] for r in conn.execute("SELECT tag FROM project_tags WHERE project_id = ? ORDER BY tag", (project_id,))]
    assert tags == ["fastapi", "python", "sqlite"]
    assert "Lights up dark dashboards" in as_role(None).get(f"/projects/{project_id}").text


def test_json_api_creates_draft(team_client):
    client, event = team_client
    response = client.post("/projects/new", json={"event": event.slug, "title": "Via API", "summary": "made by curl"})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft" and body["tagline"] == "made by curl"
    assert response.headers["location"] == f"/projects/{body['id']}"


def test_one_project_per_team(team_client):
    client, event = team_client
    assert client.post("/projects/new", json={"event": event.id, "title": "One"}).status_code == 201
    second = client.post("/projects/new", json={"event": event.id, "title": "Two"})
    assert second.status_code == 409


def test_deadline_is_enforced_to_the_second(team_client, conn):
    client, event = team_client
    project_id = client.post("/projects/new", json={"event": event.id, "title": "Clockwork"}).json()["id"]
    close = clock.parse(event.submissions_close_at)

    with clock.frozen(close - timedelta(seconds=1)):
        ok = client.post(f"/projects/{project_id}/edit", json={"tagline": "one second to spare"})
        assert ok.status_code == 200
    with clock.frozen(close):
        late = client.post(f"/projects/{project_id}/edit", json={"tagline": "exactly at the cutoff"})
        assert late.status_code == 403 and late.json()["error"] == "submissions_closed"
    with clock.frozen(close + timedelta(hours=1)):
        assert client.post(f"/projects/{project_id}/submit", json={}).status_code == 403
        upload = post_form(client, f"/projects/{project_id}/images", token_from=f"/projects/{project_id}/edit",
                           files={"image": ("shot.png", PNG, "image/png")})
        assert upload.status_code == 403
        assert client.post("/projects/new", json={"event": event.id, "title": "Too late"}).status_code == 403
    assert fetch_value(conn, "SELECT tagline FROM projects WHERE id = ?", (project_id,)) == "one second to spare"


def test_other_people_cannot_edit(team_client, seeded_app):
    client, event = team_client
    project_id = client.post("/projects/new", json={"event": event.id, "title": "Mine"}).json()["id"]
    stranger = new_user_client(seeded_app, "stranger@example.org")
    response = stranger.post(f"/projects/{project_id}/edit", json={"title": "Stolen"})
    assert response.status_code == 403


def test_links_must_be_http(team_client):
    client, event = team_client
    response = client.post("/projects/new", json={"event": event.id, "title": "XSS", "repo_url": "javascript:alert(1)"})
    assert response.status_code == 422
    assert "repo_url" in response.json()["fields"]


def test_required_custom_question_blocks_submission(team_client, conn, as_role):
    client, event = team_client
    org = as_role("organizer")
    post_form(org, f"/organize/{event.slug}/questions", {"question_prompt": "What did you learn?", "question_required": "on"},
              token_from=f"/organize/{event.slug}/settings")
    question_id = fetch_value(conn, "SELECT id FROM custom_questions WHERE event_id = ?", (event.id,))
    track = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ? LIMIT 1", (event.id,))
    project_id = client.post("/projects/new", json={
        "event": event.id, "title": "Quizzed", "tagline": "t", "track_id": track, "repo_url": "https://example.org/q",
        "description": "A project that is long enough to count as a description.",
    }).json()["id"]
    blocked = client.post(f"/projects/{project_id}/submit", json={})
    assert blocked.status_code == 422 and f"q_{question_id}" in blocked.json()["fields"]
    client.post(f"/projects/{project_id}/edit", json={f"q_{question_id}": "Deadlines are real."})
    submitted = client.post(f"/projects/{project_id}/submit", json={})
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted"


def test_uploads_are_sniffed_and_served_safely(team_client, conn):
    client, event = team_client
    project_id = client.post("/projects/new", json={"event": event.id, "title": "Pictures"}).json()["id"]
    edit = f"/projects/{project_id}/edit"
    post_form(client, f"/projects/{project_id}/images", {"caption": "screen"}, token_from=edit,
              files={"image": ("shot.png", PNG, "image/png")})
    image = fetch_one(conn, "SELECT * FROM project_images WHERE project_id = ?", (project_id,))
    assert image["file"].endswith(".png") and image["caption"] == "screen"
    served = client.get(f"/uploads/{image['file']}")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"

    for name, data in (("fake.png", b"<html>not an image</html>"),
                       ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")):
        post_form(client, f"/projects/{project_id}/images", token_from=edit, files={"image": (name, data, "image/png")})
    assert fetch_value(conn, "SELECT COUNT(*) FROM project_images WHERE project_id = ?", (project_id,)) == 1
    assert client.get("/uploads/../doggfather.db").status_code == 404
