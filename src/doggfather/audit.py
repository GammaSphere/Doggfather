"""The audit trail: append-only, hash-chained, readable by humans.

Every row stores ``hash = sha256(prev_hash + canonical_json(row))``. The
schema forbids UPDATE and DELETE, and ``verify_chain`` recomputes the chain,
so an edit made with raw SQL access (triggers dropped, rows changed) shows up
as a broken link at the exact row. Organizers read sentences, not JSON:
``describe`` turns each action into plain English.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import clock
from .db import fetch_all, fetch_value, transaction

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_payload(at, actor_id, actor_label, action, event_id, target_type, target_id, detail) -> dict[str, Any]:
    return {
        "at": at, "actor_id": actor_id, "actor_label": actor_label, "action": action,
        "event_id": event_id, "target_type": target_type, "target_id": target_id, "detail": detail,
    }


def record(db: sqlite3.Connection, action: str, *, actor=None, actor_label: str | None = None,
           event_id: str | None = None, target_type: str | None = None, target_id: str | None = None,
           detail: dict[str, Any] | None = None, ip: str | None = None) -> None:
    """Append one entry. ``actor`` is a User or None (system)."""
    label = actor_label or (actor.label if actor is not None else "system")
    detail_json = _canonical(detail or {})
    with transaction(db):
        prev = fetch_value(db, "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1", default=GENESIS)
        at = clock.now_iso()
        payload = _row_payload(at, actor.id if actor else None, label, action, event_id, target_type, target_id, detail_json)
        digest = hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO audit_log (at, actor_id, actor_label, action, event_id, target_type, target_id, detail, ip, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, actor.id if actor else None, label, action, event_id, target_type, target_id, detail_json, ip, prev, digest),
        )


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    checked: int
    broken_at: int | None = None


def verify_chain(db: sqlite3.Connection) -> ChainCheck:
    prev = GENESIS
    checked = 0
    for row in db.execute("SELECT * FROM audit_log ORDER BY id"):
        payload = _row_payload(row["at"], row["actor_id"], row["actor_label"], row["action"], row["event_id"],
                               row["target_type"], row["target_id"], row["detail"])
        expected = hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()
        if row["prev_hash"] != prev or row["hash"] != expected:
            return ChainCheck(ok=False, checked=checked, broken_at=row["id"])
        prev = row["hash"]
        checked += 1
    return ChainCheck(ok=True, checked=checked)


# Plain-English templates. Detail keys fill the placeholders; anything
# missing falls back to a generic sentence rather than failing.
SENTENCES: dict[str, str] = {
    "auth.register": "{actor} created an account",
    "auth.login": "{actor} logged in",
    "auth.login_failed": "Failed login attempt for {email}",
    "auth.logout": "{actor} logged out",
    "auth.logout_all": "{actor} signed out every other session",
    "auth.password_reset_requested": "Password reset requested for {email}",
    "auth.password_reset": "{actor} set a new password via a reset link",
    "seed.fixtures": "Fixture data loaded: {projects} projects, {judges} judges, {scores} scores",
    "event.create": "{actor} created the event “{name}”",
    "event.update": "{actor} changed event settings: {fields}",
    "event.phase": "{actor} moved {what} to {when}",
    "event.organizer_add": "{actor} made {email} an organizer",
    "track.add": "{actor} added the track “{name}”",
    "track.remove": "{actor} removed the track “{name}”",
    "prize.add": "{actor} added the prize “{name}”",
    "prize.remove": "{actor} removed the prize “{name}”",
    "question.add": "{actor} added the submission question “{prompt}”",
    "question.remove": "{actor} removed the submission question “{prompt}”",
    "team.create": "{actor} created the team “{name}”",
    "team.join": "{actor} joined the team “{name}”",
    "team.leave": "{actor} left the team “{name}”",
    "team.remove_member": "{actor} removed {member} from “{name}”",
    "team.invite_reset": "{actor} regenerated the invite link for “{name}”",
    "project.create": "{actor} started a draft: “{title}”",
    "project.update": "{actor} edited “{title}” ({fields})",
    "project.submit": "{actor} submitted “{title}”",
    "project.withdraw": "{actor} withdrew “{title}”",
    "project.refused": "Refused a change to “{title}” by {actor}: {reason}",
    "project.duplicate_flag": "“{title}” flagged as a possible duplicate of “{other}” ({reason})",
    "project.duplicate_clear": "{actor} cleared the duplicate flag on “{title}”",
    "rubric.update": "{actor} updated the rubric: {summary}",
    "judge.invite": "{actor} invited {email} to judge",
    "judge.accept": "{actor} accepted a judging invitation",
    "judge.remove": "{actor} removed judge {judge}",
    "judge.tracks": "{actor} set tracks for {judge}: {tracks}",
    "assign.auto": "{actor} ran auto-assignment batch {batch}: {created} new assignments",
    "assign.batch": "{actor} assigned {count} projects to {judge} (batch {batch})",
    "assign.remove": "{actor} removed an assignment of “{title}” from {judge}",
    "score.submit": "{actor} scored “{title}”",
    "score.update": "{actor} revised their score for “{title}”",
    "pairwise.compare": "{actor} compared two projects",
    "results.publish": "{actor} published results (method: {method})",
    "results.unpublish": "{actor} unpublished results",
    "results.method": "{actor} set the ranking method to {method}",
    "prize.award": "{actor} awarded “{prize}” to “{title}”",
    "vote.voter_verified": "A voter verified {email}",
    "vote.ballot": "Ballot cast: {votes} votes on {projects} projects ({credits} credits)",
    "vote.flag": "Voter {voter} flagged: {reason}",
    "vote.void": "{actor} voided voter {voter}",
    "vote.restore": "{actor} restored voter {voter}",
    "comment.hide": "{actor} hid a comment on “{title}”",
    "comment.restore": "{actor} restored a comment on “{title}”",
    "export.csv": "{actor} exported {kind} as CSV",
    "export.bundle": "{actor} exported the full event bundle",
    "import.bundle": "{actor} imported a bundle: {summary}",
    "webhook.create": "{actor} added a webhook to {url}",
    "webhook.delete": "{actor} removed the webhook to {url}",
    "records.issue": "{actor} issued {count} signed records ({kind})",
    "token.create": "{actor} created an API token “{name}”",
    "token.revoke": "{actor} revoked an API token “{name}”",
    "admin.toggle": "{actor} set admin={value} for {email}",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def describe(row: sqlite3.Row | dict) -> str:
    try:
        detail = json.loads(row["detail"] or "{}")
    except ValueError:
        detail = {}
    actor = row["actor_label"].split(" <")[0]
    template = SENTENCES.get(row["action"])
    values = {k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in detail.items()}
    if template:
        try:
            return template.format_map(_SafeDict(actor=actor, **values))
        except (KeyError, ValueError, IndexError):
            pass
    target = f" {row['target_type']} {row['target_id']}" if row["target_type"] else ""
    return f"{actor}: {row['action']}{target}"


def entries(db: sqlite3.Connection, *, event_id: str | None = None, limit: int = 200,
            action_prefix: str | None = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if event_id is not None:
        clauses.append("event_id = ?")
        params.append(event_id)
    if action_prefix:
        clauses.append("action LIKE ?")
        params.append(action_prefix.replace("%", "") + "%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = fetch_all(db, f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, limit))
    return [{**dict(row), "sentence": describe(row)} for row in rows]
