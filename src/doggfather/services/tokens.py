"""Personal API tokens for the REST API.

``Authorization: Bearer dgf_...`` authenticates as the token's owner with
the same permissions they have in the UI; policy checks do not care how
you logged in. Tokens are stored as SHA-256 digests and shown exactly once.
Bearer requests carry no ambient browser credentials, so they skip CSRF.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import audit, clock
from ..auth import User
from ..db import fetch_all, fetch_one, new_id, transaction
from ..errors import NotFound, ValidationFailed
from ..security import new_token, token_hash

PREFIX = "dgf_"
TOUCH_INTERVAL = timedelta(minutes=5)


def create(db: sqlite3.Connection, user: User, name: str) -> tuple[str, str]:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValidationFailed(fields={"name": "Name the token after where it will be used."})
    if len(list_tokens(db, user)) >= 20:
        raise ValidationFailed(fields={"name": "Revoke an old token first (limit 20)."})
    token = PREFIX + new_token(30)
    token_id = new_id("tok")
    with transaction(db):
        db.execute("INSERT INTO api_tokens (id, user_id, name, prefix, token_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (token_id, user.id, name, token[:10], token_hash(token), clock.now_iso()))
        audit.record(db, "token.create", actor=user, target_type="token", target_id=token_id, detail={"name": name})
    return token_id, token


def list_tokens(db: sqlite3.Connection, user: User) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT id, name, prefix, created_at, last_used_at FROM api_tokens"
                         " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC", (user.id,))


def revoke(db: sqlite3.Connection, user: User, token_id: str) -> None:
    row = fetch_one(db, "SELECT name FROM api_tokens WHERE id = ? AND user_id = ? AND revoked_at IS NULL", (token_id, user.id))
    if row is None:
        raise NotFound("No such token.")
    with transaction(db):
        db.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (clock.now_iso(), token_id))
        audit.record(db, "token.revoke", actor=user, target_type="token", target_id=token_id, detail={"name": row["name"]})


def resolve(db: sqlite3.Connection, token: str) -> User | None:
    if not token.startswith(PREFIX):
        return None
    row = fetch_one(db, "SELECT t.id AS token_id, t.last_used_at, u.* FROM api_tokens t JOIN users u ON u.id = t.user_id"
                        " WHERE t.token_hash = ? AND t.revoked_at IS NULL", (token_hash(token),))
    if row is None:
        return None
    now = clock.now()
    if row["last_used_at"] is None or now - clock.parse(row["last_used_at"]) >= TOUCH_INTERVAL:
        db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (clock.iso(now), row["token_id"]))
    return User.from_row(row)
