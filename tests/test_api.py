"""T4 + API First bonus: REST API v1 with bearer tokens and a committed OpenAPI spec."""

import json

import pytest
from fastapi.testclient import TestClient

from doggfather.db import fetch_value
from tests.conftest import ROOT
from tests.factories import open_event
from tests.helpers import post_form


@pytest.fixture
def token_for(seeded_app, as_role):
    """Create a personal token for a demo role through the UI flow."""
    def make(role: str) -> str:
        client = as_role(role)
        response = post_form(client, "/me/tokens", {"name": f"{role}-script"}, token_from="/me/tokens")
        assert response.status_code == 200
        return response.text.split('id="new-token" value="')[1].split('"')[0]
    return make


@pytest.fixture
def api(seeded_app):
    def make(token: str | None) -> TestClient:
        client = TestClient(seeded_app)
        client.__enter__()
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        return client
    return make


def test_token_lifecycle(token_for, api, conn):
    token = token_for("organizer")
    assert token.startswith("dgf_")
    assert fetch_value(conn, "SELECT COUNT(*) FROM api_tokens WHERE token_hash = ?", (token,)) == 0  # stored hashed
    client = api(token)
    me = client.get("/api/v1/me").json()
    assert me["email"] == "organizer@doggfather.local"
    token_id = client.get("/api/v1/me/tokens").json()[0]["id"]
    assert client.delete(f"/api/v1/me/tokens/{token_id}").status_code == 204
    assert client.get("/api/v1/me").status_code == 401


def test_bad_bearer_never_falls_back_to_the_cookie(api):
    client = api("dgf_not-a-real-token")
    client.cookies.set("session", "org_7f2a")  # a valid organizer cookie, ignored when a bearer is present
    assert client.get("/api/v1/me").status_code == 401


def test_bearer_writes_need_no_csrf_token(token_for, api, conn):
    client = api(token_for("organizer"))
    response = client.post("/api/v1/events/evt_01/tracks", json={"name": "From the API"})
    assert response.status_code == 201
    assert fetch_value(conn, "SELECT COUNT(*) FROM tracks WHERE name = 'From the API'") == 1


def test_api_enforces_the_same_isolation_as_the_ui(token_for, api):
    judge_b = api(token_for("judge_b"))
    assert judge_b.get("/api/v1/judge/scores").status_code == 200
    assert judge_b.get("/api/judges/jdg_26/scores").status_code == 403
    assert judge_b.get("/api/v1/events/evt_01/assignments").status_code == 403
    assert judge_b.get("/api/v1/events/evt_01/results").status_code == 403
    participant = api(token_for("participant"))
    assert participant.post("/api/v1/events/evt_01/projects", json={"title": "late"}).status_code == 403


def test_full_participant_flow_over_the_api(seeded_app, conn, api):
    event = open_event(conn)
    anon = api(None)
    anon.post("/register", data={"email": "api.person@example.org", "name": "Api Person", "password": "long enough pw",
                                 "csrf_token": anon.get("/register").text.split('name="csrf_token" value="')[1].split('"')[0]})
    token = anon.post("/api/v1/me/tokens", json={"name": "cli"}).json()["token"]
    client = api(token)
    team = client.post(f"/api/v1/events/{event.id}/teams", json={"name": "Headless"}).json()
    assert team["members"][0]["email"] == "api.person@example.org"
    track = client.get(f"/api/v1/events/{event.id}").json()["tracks"][0]["id"]
    project = client.post(f"/api/v1/events/{event.id}/projects", json={
        "title": "Headless CMS", "tagline": "No UI needed", "track_id": track, "tags": ["api", "cli"],
        "description": "Everything done over HTTP with a bearer token, start to finish.",
        "repo_url": "https://example.org/headless"}).json()
    assert project["status"] == "draft" and project["tags"] == ["api", "cli"]
    submitted = client.post(f"/api/v1/projects/{project['id']}/submit").json()
    assert submitted["status"] == "submitted"
    found = client.get("/api/v1/projects", params={"q": "headless"}).json()
    assert found["total"] == 1


def test_judge_scores_over_the_api(token_for, api, conn):
    client = api(token_for("judge_a"))
    queue = client.get("/api/v1/judge/queue", params={"event": "evt_01"}).json()
    pending = next(q["id"] for q in queue if q["status"] == "pending")
    response = client.put(f"/api/v1/judge/scores/{pending}", json={
        "criteria": {"functionality": 4, "quality": 4, "innovation": 3}, "comment": "via API"})
    assert response.status_code == 200 and response.json()["comment"] == "via API"
    bad = client.put("/api/v1/judge/scores/prj_01", json={"criteria": {"functionality": 4}})
    assert bad.status_code == 403  # not in this judge's queue


def test_organizer_manages_judging_over_the_api(token_for, api):
    client = api(token_for("organizer"))
    rubric = client.get("/api/v1/events/evt_01/rubric").json()
    assert rubric["locked"] is True and len(rubric["criteria"]) == 3
    criteria = [{**c, "weight": 2 if c["key"] == "quality" else c["weight"]} for c in rubric["criteria"]]
    updated = client.put("/api/v1/events/evt_01/rubric", json={"criteria": criteria}).json()
    assert next(c["weight"] for c in updated["criteria"] if c["key"] == "quality") == 2
    assert client.get("/api/v1/events/evt_01/progress").json()["totals"]["pending"] == 8
    assert client.get("/api/v1/events/evt_01/audit").json()["chain_ok"] is True


def test_validation_errors_share_one_envelope(token_for, api):
    client = api(token_for("organizer"))
    response = client.post("/api/v1/events", json={"name": "x"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_failed" and "fields" in body


def test_committed_openapi_matches_the_app(seeded_app):
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(seeded_app.openapi())), (
        "docs/openapi.json is stale: run `python -m doggfather openapi > docs/openapi.json`")
    assert committed["openapi"].startswith("3.")
    assert len([p for p in committed["paths"] if p.startswith("/api/v1/")]) >= 45


def test_api_reference_page_renders_offline(as_role):
    html = as_role(None).get("/api/docs").text
    assert "/api/v1/events/{event_id}/projects" in html and "cdn" not in html.lower()
