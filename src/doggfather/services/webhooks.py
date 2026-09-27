"""Outgoing webhooks: signed, retried, and never lost with the change.

Emission is a transactional outbox. ``emit`` writes one delivery row per
matching webhook inside the transaction that made the change, so a rolled
back change never announces itself and a committed one always does. A
background worker then POSTs due deliveries:

    POST <url>
    Content-Type: application/json
    X-Doggfather-Event: project.submitted
    X-Doggfather-Delivery: whd_...
    X-Doggfather-Signature: t=1760000000,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>

Receivers should recompute the HMAC, compare in constant time, and reject
timestamps older than a few minutes (replay). Failed deliveries back off
exponentially (30 s, 1 min, 2 min ... capped at 6 h) and give up after 8
attempts; the delivery log in the console shows every attempt.

Offline, deliveries to the internet simply fail and wait. Targets on the same
machine or LAN work. With ``DOGFOOD_WEBHOOK_BLOCK_PRIVATE=1``, private and
loopback targets are refused (SSRF hardening for public deployments).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import audit, clock, db as dbmod
from ..auth import User
from ..config import Settings
from ..db import fetch_all, fetch_one, new_id, transaction
from ..errors import NotFound, ValidationFailed
from ..policy import require_organizer
from .events import Event

log = logging.getLogger("doggfather.webhooks")

TOPICS = {
    "project.submitted": "A project was submitted (or resubmitted)",
    "project.updated": "A submitted project was edited",
    "team.created": "A team was formed",
    "score.submitted": "A judge filed or revised a scorecard",
    "results.published": "Results were published",
    "vote.cast": "A community ballot was cast (no voter identity)",
    "comment.created": "Someone commented on a project",
}
MAX_ATTEMPTS = 8
BATCH = 20

Transport = Callable[[str, bytes, dict[str, str]], tuple[int, str]]


def _validate_url(url: str, settings: Settings) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 500:
        raise ValidationFailed(fields={"url": "Use a full http(s):// URL."})
    if settings.webhook_block_private:
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
        except OSError:
            raise ValidationFailed(fields={"url": "That host does not resolve."}) from None
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise ValidationFailed(fields={"url": "Private and loopback targets are disabled on this server."})
    return url


def create(db: sqlite3.Connection, settings: Settings, actor: User, event: Event, url: str,
           topics: list[str]) -> tuple[str, str]:
    require_organizer(db, actor, event.id)
    url = _validate_url(url, settings)
    topics = [t for t in dict.fromkeys(topics) if t]
    unknown = [t for t in topics if t != "*" and t not in TOPICS]
    if unknown:
        raise ValidationFailed(fields={"topics": f"Unknown topic {unknown[0]!r}."})
    webhook_id = new_id("whk")
    secret = "whsec_" + secrets.token_urlsafe(24)
    with transaction(db):
        db.execute("INSERT INTO webhooks (id, event_id, url, secret, topics, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (webhook_id, event.id, url, secret, ",".join(topics) or "*", actor.id, clock.now_iso()))
        audit.record(db, "webhook.create", actor=actor, event_id=event.id, target_type="webhook", target_id=webhook_id,
                     detail={"url": url})
    return webhook_id, secret


def delete(db: sqlite3.Connection, actor: User, event: Event, webhook_id: str) -> None:
    require_organizer(db, actor, event.id)
    row = fetch_one(db, "SELECT url FROM webhooks WHERE id = ? AND event_id = ?", (webhook_id, event.id))
    if row is None:
        raise NotFound("No such webhook.")
    with transaction(db):
        db.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        audit.record(db, "webhook.delete", actor=actor, event_id=event.id, target_type="webhook", target_id=webhook_id,
                     detail={"url": row["url"]})


def list_hooks(db: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        db,
        "SELECT w.id, w.url, w.topics, w.active, w.created_at,"
        " (SELECT COUNT(*) FROM webhook_deliveries d WHERE d.webhook_id = w.id AND d.status = 'delivered') AS delivered,"
        " (SELECT COUNT(*) FROM webhook_deliveries d WHERE d.webhook_id = w.id AND d.status = 'pending') AS pending,"
        " (SELECT COUNT(*) FROM webhook_deliveries d WHERE d.webhook_id = w.id AND d.status = 'failed') AS failed"
        " FROM webhooks w WHERE w.event_id = ? ORDER BY w.created_at",
        (event_id,),
    )
    return [dict(r) for r in rows]


def deliveries(db: sqlite3.Connection, event_id: str, limit: int = 50) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT d.*, w.url FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id"
                         " WHERE w.event_id = ? ORDER BY d.created_at DESC LIMIT ?", (event_id, limit))


def emit(db: sqlite3.Connection, event_id: str, topic: str, data: dict[str, Any]) -> int:
    """Queue ``topic`` for every matching webhook. Call inside the writer's
    transaction so the announcement commits (or rolls back) with the change."""
    hooks = fetch_all(db, "SELECT id, topics FROM webhooks WHERE event_id = ? AND active = 1", (event_id,))
    now = clock.now_iso()
    queued = 0
    for hook in hooks:
        topics = hook["topics"].split(",")
        if "*" not in topics and topic not in topics:
            continue
        delivery_id = new_id("whd")
        body = {"id": delivery_id, "topic": topic, "event_id": event_id, "created_at": now, "data": data}
        db.execute("INSERT INTO webhook_deliveries (id, webhook_id, topic, payload, next_attempt_at, created_at)"
                   " VALUES (?, ?, ?, ?, ?, ?)",
                   (delivery_id, hook["id"], topic, json.dumps(body, separators=(",", ":"), ensure_ascii=False), now, now))
        queued += 1
    return queued


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_signature(secret: str, header: str, body: bytes, *, tolerance: int = 300, now: int | None = None) -> bool:
    """Reference receiver check, also used by the tests."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or int(time.time())) - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could bounce a delivery to an address the SSRF check never
    saw, so redirects are treated as failures instead of followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_transport(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(request, timeout=5) as response:  # organizer-configured URL
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)[:300]


def deliver_due(conn: sqlite3.Connection, transport: Transport = http_transport) -> int:
    """Send every due delivery once. Returns how many were attempted."""
    now = clock.now()
    rows = fetch_all(
        conn,
        "SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id"
        " WHERE d.status = 'pending' AND d.next_attempt_at <= ? ORDER BY d.next_attempt_at LIMIT ?",
        (clock.iso(now), BATCH),
    )
    for row in rows:
        body = row["payload"].encode("utf-8")
        timestamp = int(now.timestamp())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Doggfather-Webhooks/1",
            "X-Doggfather-Event": row["topic"],
            "X-Doggfather-Delivery": row["id"],
            "X-Doggfather-Signature": sign(row["secret"], timestamp, body),
        }
        status, error = transport(row["url"], body, headers)
        attempts = row["attempts"] + 1
        if 200 <= status < 300:
            conn.execute("UPDATE webhook_deliveries SET status = 'delivered', attempts = ?, last_status_code = ?,"
                         " last_error = NULL, delivered_at = ? WHERE id = ?", (attempts, status, clock.iso(now), row["id"]))
            continue
        backoff = timedelta(seconds=min(30 * 2 ** (attempts - 1), 6 * 3600))
        final = attempts >= MAX_ATTEMPTS
        conn.execute("UPDATE webhook_deliveries SET status = ?, attempts = ?, last_status_code = ?, last_error = ?,"
                     " next_attempt_at = ? WHERE id = ?",
                     ("failed" if final else "pending", attempts, status or None, error or f"HTTP {status}",
                      clock.iso(now + backoff), row["id"]))
    return len(rows)


def send_test(db: sqlite3.Connection, actor: User, event: Event, webhook_id: str) -> None:
    require_organizer(db, actor, event.id)
    hook = fetch_one(db, "SELECT * FROM webhooks WHERE id = ? AND event_id = ?", (webhook_id, event.id))
    if hook is None:
        raise NotFound("No such webhook.")
    with transaction(db):
        now = clock.now_iso()
        delivery_id = new_id("whd")
        body = {"id": delivery_id, "topic": "ping", "event_id": event.id, "created_at": now,
                "data": {"message": "Hello from Doggfather"}}
        db.execute("INSERT INTO webhook_deliveries (id, webhook_id, topic, payload, next_attempt_at, created_at)"
                   " VALUES (?, ?, 'ping', ?, ?, ?)", (delivery_id, webhook_id, json.dumps(body), now, now))


class Dispatcher:
    """Background thread that drains the outbox every couple of seconds."""

    def __init__(self, settings: Settings, interval: float = 2.0) -> None:
        self.settings = settings
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="webhook-dispatcher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                conn = dbmod.connect(self.settings.database_path)
                try:
                    deliver_due(conn)
                finally:
                    conn.close()
            except Exception:  # keep the worker alive; the next tick retries
                log.exception("webhook dispatch failed")
