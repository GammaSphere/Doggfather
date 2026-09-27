from datetime import timedelta

from doggfather import audit, clock, db
from doggfather.security import token_hash
from tests.helpers import login, post_form, register

PASSWORD = "correct horse battery"


def test_register_logs_in_and_dashboard_greets(client):
    response = register(client, "new.person@example.org", "New Person")
    assert response.status_code == 303
    assert "session" in response.cookies
    me = client.get("/me")
    assert me.status_code == 200
    assert "New" in me.text


def test_register_rejects_duplicates_and_weak_passwords(client):
    register(client, "dup@example.org")
    client.cookies.clear()
    assert register(client, "dup@example.org").status_code == 422
    assert register(client, "weak@example.org", password="short").status_code == 422


def test_login_logout_cycle_and_hashed_session_storage(client, settings):
    register(client, "cycle@example.org")
    client.cookies.clear()

    assert login(client, "cycle@example.org", "wrong password!!").status_code == 400
    response = login(client, "cycle@example.org", PASSWORD)
    assert response.status_code == 303
    token = response.cookies["session"]
    with db.connect(settings.database_path) as conn:
        stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    assert token not in stored and token_hash(token) in stored

    assert client.get("/me").status_code == 200
    post_form(client, "/logout", token_from="/me")
    client.cookies.set("session", token)  # replaying the old cookie must not work
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_protected_page_redirects_browser_to_login(client):
    response = client.get("/me", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/me")


def test_open_redirect_is_refused(client):
    register(client, "redirect@example.org")
    client.cookies.clear()
    response = post_form(client, "/login", {"email": "redirect@example.org", "password": PASSWORD, "next": "//evil.example"})
    assert response.headers["location"] == "/me"


def test_form_post_without_csrf_token_is_refused(client):
    response = client.post("/login", data={"email": "x@example.org", "password": "y"}, follow_redirects=False)
    assert response.status_code == 403


def test_cross_origin_json_post_is_refused(client):
    response = client.post("/logout", json={}, headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"


def test_login_is_rate_limited_per_account(client):
    register(client, "brute@example.org")
    client.cookies.clear()
    statuses = [login(client, "brute@example.org", f"guess-{i}-xxxxx").status_code for i in range(9)]
    assert statuses[:8] == [400] * 8
    assert statuses[8] == 429


def test_session_expires(client, settings):
    register(client, "expire@example.org")
    assert client.get("/me").status_code == 200
    with clock.frozen(clock.now() + timedelta(days=settings.session_days + 1)):
        assert client.get("/me", follow_redirects=False).status_code == 303


def test_password_reset_via_outbox_revokes_sessions(client, settings):
    register(client, "reset@example.org")
    other_session = client.cookies.get("session")
    client.cookies.clear()
    post_form(client, "/forgot", {"email": "reset@example.org"}, token_from="/forgot")
    with db.connect(settings.database_path) as conn:
        body = conn.execute("SELECT body FROM outbox WHERE to_email = 'reset@example.org'").fetchone()[0]
    link = body.split(settings.base_url)[1].split()[0]
    assert client.get(link).status_code == 200
    response = post_form(client, link, {"password": "a brand new passphrase"}, token_from=link)
    assert response.status_code == 303
    assert login(client, "reset@example.org", "a brand new passphrase").status_code == 303
    client.cookies.clear()
    client.cookies.set("session", other_session)
    assert client.get("/me", follow_redirects=False).status_code == 303
    assert client.get(link).status_code == 410  # single use


def test_forgot_does_not_reveal_accounts(client):
    known = post_form(client, "/forgot", {"email": "nobody@example.org"}, token_from="/forgot")
    assert known.status_code == 200
    assert "If an account exists" in known.text


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_audit_chain_verifies_and_detects_tampering(client, settings):
    register(client, "audit@example.org")
    with db.connect(settings.database_path) as conn:
        check = audit.verify_chain(conn)
        assert check.ok and check.checked >= 1
        # Simulate an attacker with raw database access.
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET actor_label = 'someone else' WHERE id = 1")
        broken = audit.verify_chain(conn)
    assert not broken.ok and broken.broken_at == 1


def test_audit_sentences_are_human_readable(client, settings):
    register(client, "words@example.org", "Wren Words")
    with db.connect(settings.database_path) as conn:
        latest = audit.entries(conn, limit=1)[0]
    assert latest["sentence"] == "Wren Words created an account"
