"""T4: bulk import/export (lossless round trip) and the embeddable gallery."""

import copy
import json
import re

from doggfather import db
from doggfather.db import fetch_value
from doggfather.services.bundles import export_bundle, import_bundle
from tests.conftest import make_settings
from tests.helpers import post_form

SLUG = "sample-hack-2026"
SECTIONS = ("tracks", "prizes", "questions", "criteria", "judges", "teams", "projects", "scores", "assignments")


def test_bundle_export_carries_the_whole_event(as_role):
    bundle = as_role("organizer").get("/api/v1/events/evt_01/bundle").json()
    assert bundle["format"] == "doggfather.bundle"
    assert len(bundle["projects"]) == 41 and len(bundle["scores"]) == 126 and len(bundle["assignments"]) == 8
    assert {j["id"]: j["tracks"] for j in bundle["judges"]}["jdg_26"] == ["trk_01", "trk_03"]
    assert "password" not in json.dumps(bundle)  # people's secrets never leave


def test_bundle_is_organizer_only(as_role):
    assert as_role("judge_a").get("/api/v1/events/evt_01/bundle").status_code == 403


def test_round_trip_into_a_fresh_portal_is_lossless(conn, tmp_path):
    original = export_bundle(conn, "evt_01")
    fresh_settings = make_settings(tmp_path / "fresh")
    fresh_settings.data_dir.mkdir(parents=True)
    other = db.connect(fresh_settings.database_path)
    db.migrate(other)
    try:
        report = import_bundle(other, copy.deepcopy(original))
        assert report.counts["projects"] == 41 and report.counts["scores"] == 126
        again = export_bundle(other, "evt_01")
    finally:
        other.close()
    for section in SECTIONS:
        assert again[section] == original[section], section
    for key in ("name", "submissions_close", "judging_close", "voting_mode", "vote_credits", "normalization"):
        assert again["event"][key] == original["event"][key], key


def test_import_through_the_api_matches_people_by_email(as_role, conn):
    bundle = as_role("organizer").get("/api/v1/events/evt_01/bundle").json()
    bundle["event"].update(id="evt_02", slug="sample-hack-2027", name="Sample Hack 2027")
    for section in ("tracks", "teams", "projects", "prizes", "questions"):
        for item in bundle[section]:
            item["id"] = item["id"] + "_b"
    for project in bundle["projects"]:
        project["team"] += "_b"
        project["track"] = project["track"] + "_b" if project["track"] else None
    for judge in bundle["judges"]:
        judge["tracks"] = [t + "_b" for t in judge["tracks"]]
    for card in bundle["scores"] + bundle["assignments"]:
        card["project"] += "_b"
    users_before = fetch_value(conn, "SELECT COUNT(*) FROM users")
    # The fixture's participants are on teams in evt_01 only, so they may join evt_02 teams too.
    response = as_role("organizer").post("/api/v1/bundles", json=bundle)
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["projects"] == 41
    assert fetch_value(conn, "SELECT COUNT(*) FROM users") == users_before  # nobody duplicated


def test_bad_bundles_leave_nothing_behind(as_role, conn):
    org = as_role("organizer")
    assert org.post("/api/v1/bundles", json={"projects": []}).status_code == 422
    broken = {"event": {"id": "evt_x", "name": "Broken", "submissions_close": "2026-01-01T00:00:00Z"},
              "teams": [], "projects": [{"id": "p", "team": "missing", "title": "Orphan"}]}
    assert org.post("/api/v1/bundles", json=broken).status_code == 422
    assert fetch_value(conn, "SELECT COUNT(*) FROM events WHERE id = 'evt_x'") == 0


def test_participants_cannot_import(as_role):
    assert as_role("participant").post("/api/v1/bundles", json={"event": {}}).status_code == 403


def test_import_via_the_console_upload(as_role, conn):
    bundle = as_role("organizer").get("/api/v1/events/evt_01/bundle").json()
    small = {"event": {**bundle["event"], "id": "evt_ui", "slug": "ui-import", "name": "UI Import"},
             "tracks": [{"id": "t_ui", "name": "Main"}]}
    org = as_role("organizer")
    response = post_form(org, "/organize/import", token_from="/organize",
                         files={"bundle": ("event.json", json.dumps(small).encode(), "application/json")})
    assert response.headers["location"] == "/organize/ui-import"
    assert fetch_value(conn, "SELECT COUNT(*) FROM tracks WHERE event_id = 'evt_ui'") == 1


# ------------------------------------------------------------------ embed

def test_embed_page_may_be_framed_everything_else_may_not(as_role):
    visitor = as_role(None)
    embed = visitor.get(f"/embed/events/{SLUG}?limit=3")
    assert embed.status_code == 200
    assert "frame-ancestors *" in embed.headers["content-security-policy"]
    assert "x-frame-options" not in embed.headers
    assert len(set(re.findall(r'/projects/(prj_\d+)"', embed.text))) == 3
    assert visitor.get("/projects").headers["x-frame-options"] == "DENY"


def test_embed_script_points_at_this_portal(as_role, seeded_settings):
    script = as_role(None).get("/embed.js")
    assert script.headers["content-type"].startswith("application/javascript")
    assert seeded_settings.base_url in script.text and "data-doggfather-gallery" in script.text


def test_embed_hides_drafts(as_role, conn):
    conn.execute("UPDATE projects SET status = 'draft' WHERE id = 'prj_41'")
    assert "prj_41" not in as_role(None).get(f"/embed/events/{SLUG}?limit=60").text
