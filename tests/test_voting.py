"""T3: quadratic community voting with gates, shuffled ballots, hidden tallies."""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from doggfather.db import fetch_value
from doggfather.services.voting import normalize_voter_email
from tests.helpers import login, post_form

SLUG = "sample-hack-2026"
BALLOT_ROW = re.compile(r'name="v_(prj_\d+)"')


def fresh(app) -> TestClient:
    client = TestClient(app)
    client.__enter__()
    return client


def verify_email(client, conn, email):
    """Walk the email gate and return the verified client."""
    page = f"/vote/{SLUG}"
    assert "Verify your email" in client.get(page).text
    post_form(client, f"/vote/{SLUG}/code", {"email": email}, token_from=page)
    body = fetch_value(conn, "SELECT body FROM outbox WHERE to_email = ? ORDER BY id DESC LIMIT 1", (email,))
    code = re.search(r"\b(\d{6})\b", body).group(1)
    post_form(client, f"/vote/{SLUG}/verify", {"code": code}, token_from=page)
    return client


def ballot_order(client) -> list[str]:
    return BALLOT_ROW.findall(client.get(f"/vote/{SLUG}").text)


def test_email_gate_then_quadratic_ballot(seeded_app, conn):
    voter = verify_email(fresh(seeded_app), conn, "fan@example.org")
    order = ballot_order(voter)
    assert len(order) == 41
    page = f"/vote/{SLUG}"
    response = post_form(voter, f"/vote/{SLUG}/ballot", {"v_prj_03": "4", "v_prj_05": "3"}, token_from=page)
    assert response.status_code == 303
    assert "Ballot recorded" in voter.get(page).text
    rows = dict(conn.execute("SELECT project_id, votes FROM ballot_items").fetchall())
    assert rows == {"prj_03": 4, "prj_05": 3}  # 16 + 9 = 25 credits, exactly the budget


def test_overspending_is_refused_and_leaves_the_old_ballot(seeded_app, conn):
    voter = verify_email(fresh(seeded_app), conn, "greedy@example.org")
    page = f"/vote/{SLUG}"
    post_form(voter, f"/vote/{SLUG}/ballot", {"v_prj_03": "2"}, token_from=page)
    response = post_form(voter, f"/vote/{SLUG}/ballot", {"v_prj_03": "5", "v_prj_05": "1"}, token_from=page)
    assert "26 credits" in voter.get(response.headers["location"]).text
    assert dict(conn.execute("SELECT project_id, votes FROM ballot_items").fetchall()) == {"prj_03": 2}


def test_database_trigger_enforces_the_budget(conn):
    conn.execute("INSERT INTO voters (id, event_id, kind, email, created_at) VALUES ('v1', 'evt_01', 'email', 'x@y.z', 'now')")
    conn.execute("INSERT INTO ballot_items VALUES ('v1', 'prj_01', 5, 'now')")
    with pytest.raises(sqlite3.IntegrityError, match="quadratic"):
        conn.execute("INSERT INTO ballot_items VALUES ('v1', 'prj_02', 1, 'now')")


def test_wrong_code_is_refused(seeded_app, conn):
    client = fresh(seeded_app)
    page = f"/vote/{SLUG}"
    client.get(page)
    post_form(client, f"/vote/{SLUG}/code", {"email": "guess@example.org"}, token_from=page)
    post_form(client, f"/vote/{SLUG}/verify", {"code": "000000"}, token_from=page)
    assert "Enter your code" in client.get(page).text  # still at the gate
    assert fetch_value(conn, "SELECT COUNT(*) FROM voters") == 0


def test_email_aliases_collapse_to_one_voter():
    assert normalize_voter_email("Priya.Test+hack@GMAIL.com") == normalize_voter_email("priyatest@googlemail.com")
    assert normalize_voter_email("a+b@example.org") == "a@example.org"


def test_code_requests_are_rate_limited(seeded_app):
    client = fresh(seeded_app)
    page = f"/vote/{SLUG}"
    statuses = [post_form(client, f"/vote/{SLUG}/code", {"email": "spam@example.org"}, token_from=page).status_code
                for _ in range(4)]
    assert statuses[:3] == [303, 303, 303] and statuses[3] == 429


def test_each_voter_gets_a_stable_personal_shuffle(seeded_app, conn):
    a = verify_email(fresh(seeded_app), conn, "alpha@example.org")
    b = verify_email(fresh(seeded_app), conn, "bravo@example.org")
    order_a = ballot_order(a)
    assert order_a == ballot_order(a)  # stable across reloads
    assert order_a != ballot_order(b)  # different per voter
    assert sorted(order_a) == sorted(ballot_order(b))


def test_participants_cannot_vote_for_their_own_team(as_role):
    priya = as_role("participant")  # demo accounts have verified addresses: no code needed
    html = priya.get(f"/vote/{SLUG}").text
    assert "your team" in html and 'name="v_prj_01"' not in html
    response = post_form(priya, f"/vote/{SLUG}/ballot", {"v_prj_01": "3"}, token_from=f"/vote/{SLUG}")
    assert "own team" in priya.get(response.headers["location"]).text


def test_tally_is_hidden_until_voting_closes_and_results_publish(seeded_app, conn, as_role):
    voter = verify_email(fresh(seeded_app), conn, "counted@example.org")
    post_form(voter, f"/vote/{SLUG}/ballot", {"v_prj_09": "5"}, token_from=f"/vote/{SLUG}")
    assert as_role(None).get("/api/events/evt_01/tally").status_code == 403
    assert as_role("judge_a").get("/api/events/evt_01/tally").status_code == 403
    live = as_role("organizer").get("/api/events/evt_01/tally").json()
    assert live["live"] is True and live["results"][0]["id"] == "prj_09"

    conn.execute("UPDATE events SET voting_close_at = '2026-01-01T00:00:00Z', voting_open_at = '2025-12-01T00:00:00Z',"
                 " judging_close_at = '2026-03-02T00:00:00Z', results_published_at = '2026-03-03T00:00:00Z'")
    public = as_role(None).get("/api/events/evt_01/tally")
    assert public.status_code == 200 and public.json()["results"][0]["votes"] == 5
    assert "Community choice" in as_role(None).get(f"/events/{SLUG}/results").text


def test_account_gate_requires_login(seeded_app, conn, as_role):
    conn.execute("UPDATE events SET voting_mode = 'account'")
    assert "Log in" in as_role(None).get(f"/vote/{SLUG}").text
    judge = as_role("judge_b")
    assert len(ballot_order(judge)) == 41


def test_link_mode_flags_ip_clusters_and_organizers_void(seeded_app, conn, as_role):
    conn.execute("UPDATE events SET voting_mode = 'link'")
    for i in range(5):
        device = fresh(seeded_app)
        post_form(device, f"/vote/{SLUG}/ballot", {"v_prj_12": "5"}, token_from=f"/vote/{SLUG}")
    assert fetch_value(conn, "SELECT COUNT(*) FROM voters WHERE flagged IS NOT NULL") == 5
    org = as_role("organizer")
    assert "flagged" in org.get(f"/organize/{SLUG}/votes").text
    for voter_id in [r[0] for r in conn.execute("SELECT id FROM voters")]:
        post_form(org, f"/organize/{SLUG}/votes/{voter_id}", {"action": "void"}, token_from=f"/organize/{SLUG}/votes")
    tally = org.get("/api/events/evt_01/tally").json()["results"]
    assert all(r["votes"] == 0 for r in tally)


def test_admin_outbox_is_admin_only(seeded_app, as_role):
    assert as_role("organizer").get("/admin/outbox").status_code == 403
    admin = fresh(seeded_app)
    login(admin, "admin@doggfather.local", "dogfood-demo-2026")
    assert admin.get("/admin/outbox").status_code == 200
    assert "Audit chain intact" in admin.get("/admin").text
