"""T4: outgoing webhooks: transactional outbox, HMAC signatures, retries."""

import json
import time
from datetime import timedelta

import pytest

from doggfather import auth, clock
from doggfather.db import fetch_one, fetch_value
from doggfather.errors import ValidationFailed
from doggfather.services import webhooks
from doggfather.services.events import get_event
from tests.helpers import post_form

SLUG = "sample-hack-2026"


@pytest.fixture
def hook(conn, seeded_settings):
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    webhook_id, secret = webhooks.create(conn, seeded_settings, organizer, get_event(conn, "evt_01"),
                                         "http://127.0.0.1:9/receiver", ["score.submitted", "vote.cast"])
    return webhook_id, secret


class Recorder:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.status, "" if self.status < 300 else f"HTTP {self.status}"


def score_something(as_role, conn):
    judge = as_role("judge_a")
    pid = fetch_value(conn, "SELECT project_id FROM assignments WHERE judge_id = 'jdg_26' AND status = 'pending'")
    page = f"/judge/{SLUG}/projects/{pid}"
    post_form(judge, page, {"c_evt_01:functionality": "4", "c_evt_01:quality": "4", "c_evt_01:innovation": "4"}, token_from=page)
    return pid


def test_change_and_announcement_commit_together(hook, as_role, conn):
    pid = score_something(as_role, conn)
    row = fetch_one(conn, "SELECT * FROM webhook_deliveries")
    assert row["topic"] == "score.submitted" and row["status"] == "pending"
    assert json.loads(row["payload"])["data"]["project_id"] == pid


def test_unsubscribed_topics_are_not_queued(hook, as_role, conn):
    org = as_role("organizer")
    post_form(org, f"/organize/{SLUG}/settings", {"track_name": "Nope"}, token_from=f"/organize/{SLUG}/settings")
    post_form(as_role("judge_b"), "/projects/prj_05/comments", {"body": "hello"}, token_from="/projects/prj_05")
    assert fetch_value(conn, "SELECT COUNT(*) FROM webhook_deliveries") == 0  # comment.created not subscribed


def test_signed_delivery_verifies_with_the_secret(hook, as_role, conn):
    _, secret = hook
    score_something(as_role, conn)
    recorder = Recorder()
    assert webhooks.deliver_due(conn, recorder) == 1
    url, body, headers = recorder.calls[0]
    assert headers["X-Doggfather-Event"] == "score.submitted"
    assert webhooks.verify_signature(secret, headers["X-Doggfather-Signature"], body)
    assert not webhooks.verify_signature("wrong-secret", headers["X-Doggfather-Signature"], body)
    assert not webhooks.verify_signature(secret, headers["X-Doggfather-Signature"], body + b" ")
    assert fetch_value(conn, "SELECT status FROM webhook_deliveries") == "delivered"


def test_stale_signatures_are_rejected():
    body = b'{"x":1}'
    header = webhooks.sign("s", int(time.time()) - 3600, body)
    assert not webhooks.verify_signature("s", header, body)


def test_failures_back_off_then_give_up(hook, as_role, conn):
    score_something(as_role, conn)
    failing = Recorder(status=500)
    webhooks.deliver_due(conn, failing)
    row = fetch_one(conn, "SELECT * FROM webhook_deliveries")
    assert row["status"] == "pending" and row["attempts"] == 1 and row["last_status_code"] == 500
    assert webhooks.deliver_due(conn, failing) == 0  # not due yet: backing off
    for _ in range(webhooks.MAX_ATTEMPTS):
        with clock.frozen(clock.now() + timedelta(hours=7)):
            webhooks.deliver_due(conn, failing)
        conn.execute("UPDATE webhook_deliveries SET next_attempt_at = '2000-01-01T00:00:00Z' WHERE status = 'pending'")
    assert fetch_value(conn, "SELECT status FROM webhook_deliveries") == "failed"


def test_bad_urls_and_topics_are_refused(conn, seeded_settings):
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    event = get_event(conn, "evt_01")
    with pytest.raises(ValidationFailed):
        webhooks.create(conn, seeded_settings, organizer, event, "file:///etc/passwd", ["*"])
    with pytest.raises(ValidationFailed):
        webhooks.create(conn, seeded_settings, organizer, event, "https://example.org/x", ["everything.ever"])


def test_private_targets_can_be_blocked(conn, seeded_settings):
    from dataclasses import replace

    strict = replace(seeded_settings, webhook_block_private=True)
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    with pytest.raises(ValidationFailed) as refused:
        webhooks.create(conn, strict, organizer, get_event(conn, "evt_01"), "http://127.0.0.1:8080/hook", ["*"])
    assert "Private" in refused.value.fields["url"]


def test_webhook_console_and_secret_shown_once(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/webhooks"
    response = post_form(org, page, {"url": "https://hooks.example.org/in", "topics": ["project.submitted"]}, token_from=page)
    assert "whsec_" in response.text
    assert "whsec_" not in org.get(page).text
    assert as_role("judge_a").get(page).status_code == 403
