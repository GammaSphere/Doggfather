"""Judge invitations, track coverage and the judge roster."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .. import audit, clock
from ..auth import User, normalize_email, validate_email
from ..config import Settings
from ..db import fetch_all, fetch_one, new_id, placeholders, transaction
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..policy import require_organizer
from ..security import new_token, token_hash
from . import mailer
from .events import Event, get_event

INVITE_DAYS = 14


def roster(db: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    """Judges with their tracks and workload. Organizer-only data."""
    rows = fetch_all(
        db,
        "SELECT u.id, u.name, u.email, u.password_hash IS NOT NULL AS has_password,"
        " (SELECT COUNT(*) FROM assignments a WHERE a.judge_id = u.id AND a.event_id = m.event_id) AS assigned,"
        " (SELECT COUNT(*) FROM assignments a WHERE a.judge_id = u.id AND a.event_id = m.event_id AND a.status = 'done') AS done,"
        " (SELECT MAX(s.updated_at) FROM scores s WHERE s.judge_id = u.id AND s.event_id = m.event_id) AS last_scored_at"
        " FROM event_members m JOIN users u ON u.id = m.user_id WHERE m.event_id = ? AND m.role = 'judge'"
        " ORDER BY u.name",
        (event_id,),
    )
    tracks = fetch_all(
        db,
        "SELECT jt.user_id, t.id, t.name FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id"
        " WHERE jt.event_id = ? ORDER BY t.position",
        (event_id,),
    )
    by_judge: dict[str, list[dict[str, str]]] = {}
    for row in tracks:
        by_judge.setdefault(row["user_id"], []).append({"id": row["id"], "name": row["name"]})
    return [{**dict(r), "tracks": by_judge.get(r["id"], [])} for r in rows]


def pending_invites(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT * FROM invites WHERE event_id = ? AND accepted_at IS NULL AND expires_at > ?"
                         " ORDER BY created_at DESC", (event_id, clock.now_iso()))


def _valid_tracks(db: sqlite3.Connection, event_id: str, track_ids: list[str]) -> list[str]:
    track_ids = [t for t in dict.fromkeys(track_ids) if t]
    if not track_ids:
        return []
    found = {r["id"] for r in fetch_all(db, f"SELECT id FROM tracks WHERE event_id = ? AND id IN ({placeholders(track_ids)})",
                                        (event_id, *track_ids))}
    if len(found) != len(track_ids):
        raise ValidationFailed(fields={"tracks": "Pick tracks from this event."})
    return track_ids


def add_judge(db: sqlite3.Connection, event: Event, user: User, track_ids: list[str]) -> None:
    """Seat an existing account as a judge (used by invite acceptance)."""
    track_ids = _valid_tracks(db, event.id, track_ids)
    if fetch_one(db, "SELECT 1 FROM team_members WHERE event_id = ? AND user_id = ?", (event.id, user.id)):
        raise Forbidden("This account is on a team in this event, so it cannot judge it.", code="conflict_of_interest")
    if fetch_one(db, "SELECT 1 FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'organizer'",
                 (event.id, user.id)):
        raise Conflict("Organizers already see everything; they do not need a judge seat.")
    now = clock.now_iso()
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'judge', ?)", (event.id, user.id, now))
        db.executemany("INSERT OR IGNORE INTO judge_tracks VALUES (?, ?, ?)", [(event.id, user.id, t) for t in track_ids])


def invite_judge(db: sqlite3.Connection, settings: Settings, actor: User, event: Event, email: str,
                 track_ids: list[str], note: str = "") -> str:
    require_organizer(db, actor, event.id)
    email = normalize_email(email)
    error = validate_email(email)
    if error:
        raise ValidationFailed(fields={"email": error})
    track_ids = _valid_tracks(db, event.id, track_ids)
    if fetch_one(db, "SELECT 1 FROM event_members m JOIN users u ON u.id = m.user_id"
                     " WHERE m.event_id = ? AND u.email = ? AND m.role = 'judge'", (event.id, email)):
        raise Conflict("That person already judges this event. Adjust their tracks on the roster instead.")
    token = new_token()
    invite_id = new_id("inv")
    now = clock.now()
    with transaction(db):
        db.execute(
            "INSERT INTO invites (id, event_id, email, role, track_ids, token_hash, invited_by, created_at, expires_at)"
            " VALUES (?, ?, ?, 'judge', ?, ?, ?, ?, ?)",
            (invite_id, event.id, email, json.dumps(track_ids), token_hash(token), actor.id, clock.iso(now),
             clock.iso(now + timedelta(days=INVITE_DAYS))),
        )
        track_names = [r["name"] for r in fetch_all(
            db, f"SELECT name FROM tracks WHERE id IN ({placeholders(track_ids)})", track_ids)] if track_ids else []
        body = (
            f"{actor.name} invited you to judge {event.name}.\n\n"
            + (f"Tracks: {', '.join(track_names)}\n\n" if track_names else "")
            + (f"{note.strip()}\n\n" if note.strip() else "")
            + f"Accept here (link valid {INVITE_DAYS} days):\n{settings.base_url}/invite/{token}\n"
        )
        mailer.send(db, settings, email, f"Judge {event.name}", body)
        audit.record(db, "judge.invite", actor=actor, event_id=event.id, target_type="invite", target_id=invite_id,
                     detail={"email": email})
    return token


def invite_for_token(db: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = fetch_one(db, "SELECT * FROM invites WHERE token_hash = ?", (token_hash(token),))
    if row is None or row["expires_at"] <= clock.now_iso():
        raise NotFound("This invitation is not valid or has expired. Ask the organizers for a new one.")
    return row


def accept_invite(db: sqlite3.Connection, user: User, token: str) -> Event:
    invite = invite_for_token(db, token)
    if invite["accepted_at"]:
        if invite["accepted_by"] == user.id:
            return get_event(db, invite["event_id"])
        raise Conflict("This invitation was already used.")
    event = get_event(db, invite["event_id"])
    with transaction(db):
        add_judge(db, event, user, json.loads(invite["track_ids"] or "[]"))
        db.execute("UPDATE invites SET accepted_at = ?, accepted_by = ? WHERE id = ?", (clock.now_iso(), user.id, invite["id"]))
        audit.record(db, "judge.accept", actor=user, event_id=event.id, target_type="invite", target_id=invite["id"])
    return event


def set_tracks(db: sqlite3.Connection, actor: User, event: Event, judge_id: str, track_ids: list[str]) -> None:
    require_organizer(db, actor, event.id)
    track_ids = _valid_tracks(db, event.id, track_ids)
    judge = fetch_one(db, "SELECT u.name FROM event_members m JOIN users u ON u.id = m.user_id"
                          " WHERE m.event_id = ? AND m.user_id = ? AND m.role = 'judge'", (event.id, judge_id))
    if judge is None:
        raise NotFound("No such judge in this event.")
    with transaction(db):
        db.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event.id, judge_id))
        db.executemany("INSERT INTO judge_tracks VALUES (?, ?, ?)", [(event.id, judge_id, t) for t in track_ids])
        names = [r["name"] for r in fetch_all(db, f"SELECT name FROM tracks WHERE id IN ({placeholders(track_ids)})",
                                              track_ids)] if track_ids else ["none"]
        audit.record(db, "judge.tracks", actor=actor, event_id=event.id, target_type="user", target_id=judge_id,
                     detail={"judge": judge["name"], "tracks": names})


def remove_judge(db: sqlite3.Connection, actor: User, event: Event, judge_id: str) -> None:
    require_organizer(db, actor, event.id)
    judge = fetch_one(db, "SELECT u.name FROM event_members m JOIN users u ON u.id = m.user_id"
                          " WHERE m.event_id = ? AND m.user_id = ? AND m.role = 'judge'", (event.id, judge_id))
    if judge is None:
        raise NotFound("No such judge in this event.")
    if fetch_one(db, "SELECT 1 FROM scores WHERE event_id = ? AND judge_id = ?", (event.id, judge_id)):
        raise Conflict("This judge has filed scores; their record stays. Remove their pending assignments instead.")
    with transaction(db):
        db.execute("DELETE FROM assignments WHERE event_id = ? AND judge_id = ?", (event.id, judge_id))
        db.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event.id, judge_id))
        db.execute("DELETE FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'judge'", (event.id, judge_id))
        audit.record(db, "judge.remove", actor=actor, event_id=event.id, target_type="user", target_id=judge_id,
                     detail={"judge": judge["name"]})
