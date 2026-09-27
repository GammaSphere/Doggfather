"""T1: team formation by invite link."""

from doggfather.db import fetch_all, fetch_one, fetch_value
from tests.factories import new_user_client, open_event
from tests.helpers import post_form


def team_of(conn, email, event_id):
    return fetch_one(conn, "SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id"
                           " WHERE u.email = ? AND m.event_id = ?", (email, event_id))


def test_invite_link_flow_and_size_cap(seeded_app, conn):
    event = open_event(conn, max_team_size=3)
    captain = new_user_client(seeded_app, "cap@example.org", "Cap Tain")
    team_page = f"/events/{event.slug}/team"
    assert post_form(captain, team_page, {"team_name": "Night Owls"}, token_from=team_page).status_code == 303
    team = team_of(conn, "cap@example.org", event.id)
    assert team["name"] == "Night Owls"
    assert f"/join/{team['invite_code']}" in captain.get(team_page).text

    for i in (1, 2):
        mate = new_user_client(seeded_app, f"mate{i}@example.org")
        assert mate.get(f"/join/{team['invite_code']}").status_code == 200
        post_form(mate, f"/join/{team['invite_code']}", token_from=f"/join/{team['invite_code']}")
    assert fetch_value(conn, "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],)) == 3

    late = new_user_client(seeded_app, "late@example.org")
    response = post_form(late, f"/join/{team['invite_code']}", token_from=f"/join/{team['invite_code']}")
    assert "full" in late.get(response.headers["location"]).text
    assert fetch_value(conn, "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],)) == 3


def test_one_team_per_event(seeded_app, conn):
    event = open_event(conn)
    a = new_user_client(seeded_app, "a@example.org")
    b = new_user_client(seeded_app, "b@example.org")
    page = f"/events/{event.slug}/team"
    post_form(a, page, {"team_name": "Team A"}, token_from=page)
    post_form(b, page, {"team_name": "Team B"}, token_from=page)
    code_a = team_of(conn, "a@example.org", event.id)["invite_code"]
    post_form(b, f"/join/{code_a}", token_from=f"/join/{code_a}")
    assert team_of(conn, "b@example.org", event.id)["name"] == "Team B"


def test_judges_cannot_join_teams_in_their_event(seeded_app, conn, as_role):
    event = open_event(conn)
    conn.execute("INSERT INTO event_members VALUES (?, 'jdg_26', 'judge', '2026-01-01T00:00:00Z')", (event.id,))
    judge = as_role("judge_a")
    page = f"/events/{event.slug}/team"
    response = post_form(judge, page, {"team_name": "Sneaky"}, token_from=page)
    assert "cannot join a team" in judge.get(response.headers["location"]).text
    assert team_of(conn, "jonas.vogel@example.org", event.id) is None


def test_teams_lock_after_the_deadline(as_role, conn):
    # The fixture event closed on 2026-03-01; its teams are frozen.
    participant = as_role("participant")
    code = fetch_value(conn, "SELECT invite_code FROM teams WHERE id = 'tm_02'")
    response = post_form(participant, f"/join/{code}", token_from=f"/join/{code}")
    assert "locked" in participant.get(response.headers["location"]).text
    assert team_of(conn, "priya1@example.org", "evt_01")["id"] == "tm_01"


def test_captain_leaving_hands_over_captaincy(seeded_app, conn):
    event = open_event(conn)
    cap = new_user_client(seeded_app, "first@example.org")
    page = f"/events/{event.slug}/team"
    post_form(cap, page, {"team_name": "Relay"}, token_from=page)
    team = team_of(conn, "first@example.org", event.id)
    mate = new_user_client(seeded_app, "second@example.org")
    post_form(mate, f"/join/{team['invite_code']}", token_from=f"/join/{team['invite_code']}")
    post_form(cap, f"/teams/{team['id']}/leave", token_from=page)
    rows = fetch_all(conn, "SELECT u.email, m.role FROM team_members m JOIN users u ON u.id = m.user_id WHERE m.team_id = ?", (team["id"],))
    assert [(r["email"], r["role"]) for r in rows] == [("second@example.org", "captain")]


def test_regenerated_invite_kills_the_old_link(seeded_app, conn):
    event = open_event(conn)
    cap = new_user_client(seeded_app, "regen@example.org")
    page = f"/events/{event.slug}/team"
    post_form(cap, page, {"team_name": "Rotators"}, token_from=page)
    team = team_of(conn, "regen@example.org", event.id)
    post_form(cap, f"/teams/{team['id']}/invite", token_from=page)
    assert cap.get(f"/join/{team['invite_code']}").status_code == 404


def test_only_the_captain_removes_members(seeded_app, conn):
    event = open_event(conn)
    cap = new_user_client(seeded_app, "boss@example.org")
    page = f"/events/{event.slug}/team"
    post_form(cap, page, {"team_name": "Crew"}, token_from=page)
    team = team_of(conn, "boss@example.org", event.id)
    mate = new_user_client(seeded_app, "crew@example.org")
    post_form(mate, f"/join/{team['invite_code']}", token_from=f"/join/{team['invite_code']}")
    boss_id = fetch_value(conn, "SELECT id FROM users WHERE email = 'boss@example.org'")
    post_form(mate, f"/teams/{team['id']}/members/{boss_id}/remove", token_from=page)
    assert fetch_value(conn, "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],)) == 2
