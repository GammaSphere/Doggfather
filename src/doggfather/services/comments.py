"""Public comments on submitted projects, with organizer moderation.

Hidden comments are kept, not deleted: moderation is reversible and every
hide or restore is in the audit log.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import audit, clock, policy
from ..auth import User
from ..db import fetch_all, fetch_one, new_id, transaction
from ..errors import Conflict, NotFound, ValidationFailed
from .projects import get_project

MAX_LENGTH = 2000


def list_comments(db: sqlite3.Connection, project_id: str, *, include_hidden: bool = False) -> list[sqlite3.Row]:
    hidden = "" if include_hidden else " AND c.hidden_at IS NULL"
    return fetch_all(
        db,
        "SELECT c.*, u.name AS author FROM comments c JOIN users u ON u.id = c.user_id"
        f" WHERE c.project_id = ?{hidden} ORDER BY c.created_at",
        (project_id,),
    )


def add_comment(db: sqlite3.Connection, user: User, project_id: str, body: str, ip_hash: str | None = None) -> str:
    project = get_project(db, project_id)
    if project["status"] != "submitted":
        raise NotFound("No such project.")
    body = (body or "").strip()
    if not body:
        raise ValidationFailed(fields={"body": "Write something first."})
    if len(body) > MAX_LENGTH:
        raise ValidationFailed(fields={"body": f"Keep comments under {MAX_LENGTH} characters."})
    since = clock.iso(clock.now() - timedelta(minutes=10))
    if fetch_one(db, "SELECT 1 FROM comments WHERE user_id = ? AND project_id = ? AND body = ? AND created_at > ?",
                 (user.id, project_id, body, since)):
        raise Conflict("You just posted that.", code="duplicate_comment")
    comment_id = new_id("cmt")
    db.execute("INSERT INTO comments (id, project_id, user_id, body, created_at, ip_hash) VALUES (?, ?, ?, ?, ?, ?)",
               (comment_id, project_id, user.id, body, clock.now_iso(), ip_hash))
    return comment_id


def set_hidden(db: sqlite3.Connection, actor: User, comment_id: str, hidden: bool, reason: str = "") -> sqlite3.Row:
    row = fetch_one(db, "SELECT c.*, p.event_id, p.title FROM comments c JOIN projects p ON p.id = c.project_id"
                        " WHERE c.id = ?", (comment_id,))
    if row is None:
        raise NotFound("No such comment.")
    policy.require_organizer(db, actor, row["event_id"])
    with transaction(db):
        if hidden:
            db.execute("UPDATE comments SET hidden_at = ?, hidden_by = ?, hidden_reason = ? WHERE id = ?",
                       (clock.now_iso(), actor.id, reason.strip()[:200] or None, comment_id))
        else:
            db.execute("UPDATE comments SET hidden_at = NULL, hidden_by = NULL, hidden_reason = NULL WHERE id = ?",
                       (comment_id,))
        audit.record(db, "comment.hide" if hidden else "comment.restore", actor=actor, event_id=row["event_id"],
                     target_type="comment", target_id=comment_id, detail={"title": row["title"]})
    return row


def recent_for_event(db: sqlite3.Connection, event_id: str, limit: int = 50) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT c.*, u.name AS author, p.title FROM comments c JOIN users u ON u.id = c.user_id"
        " JOIN projects p ON p.id = c.project_id WHERE p.event_id = ? ORDER BY c.created_at DESC LIMIT ?",
        (event_id, limit),
    )
