"""Fixture import: every record lands, awkward cases survive intact."""

import pytest

from doggfather import audit
from doggfather.db import fetch_all, fetch_value
from doggfather.seed import seed
from doggfather.services.bundles import import_bundle
from doggfather.errors import Conflict
from tests.conftest import FIXTURES


def count(conn, table, where="1=1", params=()):
    return fetch_value(conn, f"SELECT COUNT(*) FROM {table} WHERE {where}", params)


def test_every_fixture_record_is_loaded(conn):
    assert count(conn, "events") == 1
    assert count(conn, "tracks") == len(FIXTURES["tracks"]) == 8
    assert count(conn, "event_members", "role = 'judge'") == len(FIXTURES["judges"]) == 30
    assert count(conn, "teams") == len(FIXTURES["teams"]) == 40
    assert count(conn, "projects", "status = 'submitted'") == len(FIXTURES["projects"]) == 41
    assert count(conn, "scores") == len(FIXTURES["scores"]) == 126
    assert count(conn, "score_items") == 126 * 3
    assert [r["key"] for r in fetch_all(conn, "SELECT key FROM criteria ORDER BY position")] == [
        "functionality", "quality", "innovation"]


def test_fixture_close_date_is_kept_exactly(conn):
    assert fetch_value(conn, "SELECT submissions_close_at FROM events") == FIXTURES["event"]["submissions_close"]


def test_ids_and_timestamps_are_preserved(conn):
    row = conn.execute("SELECT * FROM projects WHERE id = 'prj_41'").fetchone()
    assert row["team_id"] == "tm_07" and row["title"] == "Dry Harbour"
    assert row["submitted_at"] == "2026-03-01T17:57:00Z"


def test_repeated_team_names_are_allowed(conn):
    assert count(conn, "teams", "name = 'StillTrail'") == 3


def test_judge_tracks_match_fixture(conn):
    tracks = {r["track_id"] for r in fetch_all(conn, "SELECT track_id FROM judge_tracks WHERE user_id = 'jdg_26'")}
    assert tracks == {"trk_03", "trk_01"}


def test_flat_judge_scores_survive(conn):
    values = {r[0] for r in conn.execute(
        "SELECT i.value FROM score_items i JOIN scores s ON s.id = i.score_id WHERE s.judge_id = 'jdg_07'")}
    assert values == {4}


def test_demo_sessions_authenticate_the_documented_people(as_role):
    assert "Olu" in as_role("organizer").get("/me").text
    assert "Jonas" in as_role("judge_a").get("/me").text
    assert "Diego" in as_role("judge_b").get("/me").text
    assert "Priya" in as_role("participant").get("/me").text


def test_seed_is_idempotent(conn, seeded_settings):
    result = seed(conn, seeded_settings, FIXTURES)
    assert result.seeded is False
    assert count(conn, "projects") == 41


def test_reimporting_the_same_event_is_refused(conn):
    with pytest.raises(Conflict):
        import_bundle(conn, FIXTURES)
    assert count(conn, "projects") == 41  # nothing partial


def test_seed_is_audited(conn):
    sentences = [e["sentence"] for e in audit.entries(conn)]
    assert any("Fixture data loaded: 41 projects, 30 judges, 126 scores" in s for s in sentences)
    assert audit.verify_chain(conn).ok
