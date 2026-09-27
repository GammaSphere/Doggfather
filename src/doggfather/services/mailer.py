"""Outgoing mail, offline first.

Every message is written to the ``outbox`` table, which admins can read in
the UI; that is how invitations, voter codes and password resets reach
people in a portal running without a network. If ``DOGFOOD_SMTP_HOST`` is
set, messages are also delivered by SMTP from a background thread.
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
import threading
from email.message import EmailMessage

from .. import clock, db
from ..config import Settings

log = logging.getLogger("doggfather.mail")


def send(conn: sqlite3.Connection, settings: Settings, to: str, subject: str, body: str) -> int:
    cursor = conn.execute(
        "INSERT INTO outbox (to_email, subject, body, created_at) VALUES (?, ?, ?, ?)",
        (to, subject, body, clock.now_iso()),
    )
    message_id = int(cursor.lastrowid or 0)
    if settings.smtp_host:
        threading.Thread(target=_deliver, args=(settings, message_id, to, subject, body), daemon=True).start()
    return message_id


def _deliver(settings: Settings, message_id: int, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    error = None
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        error = str(exc)[:500]
        log.warning("mail %s to %s failed: %s", message_id, to, error)
    conn = db.connect(settings.database_path)
    try:
        conn.execute("UPDATE outbox SET sent_at = ?, error = ? WHERE id = ?",
                     (None if error else clock.now_iso(), error, message_id))
    finally:
        conn.close()


def recent(conn: sqlite3.Connection, limit: int = 100, to: str | None = None) -> list[sqlite3.Row]:
    if to:
        return db.fetch_all(conn, "SELECT * FROM outbox WHERE to_email = ? ORDER BY id DESC LIMIT ?", (to, limit))
    return db.fetch_all(conn, "SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
