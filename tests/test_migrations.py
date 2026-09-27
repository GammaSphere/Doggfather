"""The schema enforces its own invariants, independent of the service layer."""

import sqlite3

import pytest

from doggfather import db

T = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "schema.db")
    db.migrate(connection)
    yield connection
    connection.close()


def _event(conn, event_id="evt_x"):
    conn.execute(
        "INSERT INTO events (id, slug, name, submissions_open_at, submissions_close_at,"
        " judging_close_at, created_at, updated_at) VALUES (?, ?, 'E', ?, ?, ?, ?, ?)",
        (event_id, event_id, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-10T00:00:00Z", T, T),
    )


def _user(conn, user_id):
    conn.execute("INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
                 (user_id, f"{user_id}@example.org", user_id, T))


def test_migrations_are_idempotent(conn):
    assert db.migrate(conn) == []


def test_event_dates_must_be_ordered(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (id, slug, name, submissions_open_at, submissions_close_at,"
            " judging_close_at, created_at, updated_at) VALUES ('e', 'e', 'E', ?, ?, ?, ?, ?)",
            ("2026-01-05T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-10T00:00:00Z", T, T),
        )


def test_one_team_per_person_per_event(conn):
    _event(conn)
    _user(conn, "u1")
    for team in ("t1", "t2"):
        conn.execute("INSERT INTO teams (id, event_id, name, invite_code, created_at) VALUES (?, 'evt_x', 'Same name', ?, ?)",
                     (team, team + "code", T))
    conn.execute("INSERT INTO team_members (team_id, event_id, user_id, joined_at) VALUES ('t1', 'evt_x', 'u1', ?)", (T,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO team_members (team_id, event_id, user_id, joined_at) VALUES ('t2', 'evt_x', 'u1', ?)", (T,))


def test_team_member_event_must_match_team(conn):
    _event(conn)
    _event(conn, "evt_y")
    _user(conn, "u1")
    conn.execute("INSERT INTO teams (id, event_id, name, invite_code, created_at) VALUES ('t1', 'evt_x', 'A', 'c1', ?)", (T,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO team_members (team_id, event_id, user_id, joined_at) VALUES ('t1', 'evt_y', 'u1', ?)", (T,))


def test_judge_cannot_also_participate(conn):
    _event(conn)
    _user(conn, "u1")
    conn.execute("INSERT INTO event_members VALUES ('evt_x', 'u1', 'judge', ?)", (T,))
    with pytest.raises(sqlite3.IntegrityError, match="conflict of interest"):
        conn.execute("INSERT INTO event_members VALUES ('evt_x', 'u1', 'participant', ?)", (T,))


def test_score_outside_rubric_range_is_rejected(conn):
    _event(conn)
    _user(conn, "j1")
    conn.execute("INSERT INTO teams (id, event_id, name, invite_code, created_at) VALUES ('t1', 'evt_x', 'A', 'c1', ?)", (T,))
    conn.execute("INSERT INTO projects (id, event_id, team_id, title, created_at, updated_at) VALUES ('p1', 'evt_x', 't1', 'P', ?, ?)", (T, T))
    conn.execute("INSERT INTO criteria (id, event_id, key, label) VALUES ('c1', 'evt_x', 'fun', 'Fun')")
    conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, batch, assigned_at) VALUES ('a1', 'evt_x', 'j1', 'p1', 'b', ?)", (T,))
    conn.execute("INSERT INTO scores (id, assignment_id, event_id, judge_id, project_id, submitted_at, updated_at) VALUES ('s1', 'a1', 'evt_x', 'j1', 'p1', ?, ?)", (T, T))
    conn.execute("INSERT INTO score_items VALUES ('s1', 'c1', 5)")
    with pytest.raises(sqlite3.IntegrityError, match="range"):
        conn.execute("UPDATE score_items SET value = 6 WHERE score_id = 's1'")


def test_audit_log_is_append_only(conn):
    conn.execute("INSERT INTO audit_log (at, actor_label, action, prev_hash, hash) VALUES (?, 'system', 'x', '', 'h1')", (T,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET action = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_transaction_rolls_back_and_nests(conn):
    _user(conn, "u1")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("UPDATE users SET name = 'changed' WHERE id = 'u1'")
            with db.transaction(conn):
                conn.execute("UPDATE users SET name = 'inner' WHERE id = 'u1'")
            raise RuntimeError("boom")
    assert db.fetch_value(conn, "SELECT name FROM users WHERE id = 'u1'") == "u1"
