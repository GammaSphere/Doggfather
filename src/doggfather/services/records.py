"""Certificates and signed judge records.

Three kinds of record, each a canonical JSON payload plus an Ed25519
signature:

``judge_record``
    Proves a judge served an event and filed N reviews. It includes
    ``scores_sha256``, a SHA-256 commitment to the judge's exact scorecards,
    so the record reveals nothing about how they scored. Later, the judge
    (or an organizer) can disclose the scorecards and anyone can check that
    they hash to the committed value.
``participation``
    One per member of every team with a submitted project.
``award``
    One per member of a team that won a prize.

Records are immutable (a trigger enforces it). A correction revokes the old
record and issues a new one; verification reports revocations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import audit, clock, policy
from ..auth import User
from ..config import Settings
from ..db import fetch_all, fetch_one, new_id, transaction
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from . import scoring, signing
from .events import Event

KIND_LABELS = {"judge_record": "Judge record", "participation": "Participation", "award": "Award"}


def signer(settings: Settings) -> signing.Signer:
    return signing.load_signer(settings.keys_dir)


def scores_commitment(scorecards: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical, sorted list of (project, criteria) pairs."""
    material = sorted(({"project": s["project_id"], "criteria": s["criteria"]} for s in scorecards),
                      key=lambda item: item["project"])
    return hashlib.sha256(signing.canonical({"scorecards": material})).hexdigest()


def _issue(db: sqlite3.Connection, settings: Settings, kind: str, event: Event, subject_id: str | None,
           subject_name: str, subject_key: str, body: dict[str, Any]) -> str | None:
    """Sign and store; if an identical live record exists, keep it. If the
    content changed (more reviews filed since), revoke and reissue."""
    s = signer(settings)
    live = fetch_one(db, "SELECT * FROM records WHERE subject_key = ? AND revoked_at IS NULL", (subject_key,))
    if live:
        previous = json.loads(live["payload"])
        if {k: v for k, v in previous.items() if k not in ("issued_at", "record_id")} == body:
            return None
        db.execute("UPDATE records SET revoked_at = ?, revoked_reason = 'superseded' WHERE id = ?",
                   (clock.now_iso(), live["id"]))
    record_id = new_id("rec")
    payload = {**body, "record_id": record_id, "issued_at": clock.now_iso()}
    db.execute(
        "INSERT INTO records (id, kind, event_id, subject_user_id, subject_name, subject_key, payload, signature, key_id,"
        " issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record_id, kind, event.id, subject_id, subject_name, subject_key,
         signing.canonical(payload).decode("utf-8"), s.sign(payload), s.key_id, payload["issued_at"]),
    )
    return record_id


def _base(settings: Settings, kind: str, event: Event) -> dict[str, Any]:
    return {"type": f"doggfather.{kind}", "version": 1, "issuer": settings.base_url,
            "key_id": signer(settings).key_id, "event": {"id": event.id, "name": event.name}}


def issue_judge_records(db: sqlite3.Connection, settings: Settings, actor: User, event: Event) -> int:
    policy.require_organizer(db, actor, event.id)
    if clock.now_iso() < event.judging_close_at:
        raise Conflict("Judge records are issued once judging has closed.")
    judges = fetch_all(db, "SELECT DISTINCT u.id, u.name FROM scores s JOIN users u ON u.id = s.judge_id"
                           " WHERE s.event_id = ? ORDER BY u.name", (event.id,))
    issued = 0
    with transaction(db):
        for judge in judges:
            cards = scoring.scores_for_judge(db, judge["id"], event.id)
            tracks = [r["name"] for r in fetch_all(db, "SELECT t.name FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id"
                                                       " WHERE jt.user_id = ? AND jt.event_id = ? ORDER BY t.position",
                                                   (judge["id"], event.id))]
            body = {**_base(settings, "judge_record", event), "judge": {"id": judge["id"], "name": judge["name"]},
                    "reviews": len(cards), "tracks": tracks, "scores_sha256": scores_commitment(cards)}
            if _issue(db, settings, "judge_record", event, judge["id"], judge["name"],
                      f"judge_record:{event.id}:{judge['id']}", body):
                issued += 1
        audit.record(db, "records.issue", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"count": issued, "kind": "judge records"})
    return issued


def issue_certificates(db: sqlite3.Connection, settings: Settings, actor: User, event: Event) -> int:
    policy.require_organizer(db, actor, event.id)
    if not event.submissions_closed():
        raise Conflict("Certificates are issued after submissions close.")
    rows = fetch_all(
        db,
        "SELECT u.id, u.name, tm.name AS team_name, p.id AS project_id, p.title FROM projects p"
        " JOIN teams tm ON tm.id = p.team_id JOIN team_members m ON m.team_id = tm.id JOIN users u ON u.id = m.user_id"
        " WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY u.id, p.duplicate_of IS NOT NULL, p.submitted_at",
        (event.id,),
    )
    # One participation certificate per person: a team with a flagged duplicate
    # entry must not churn certificates between its two projects.
    members, seen = [], set()
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            members.append(row)
    awards = fetch_all(
        db,
        "SELECT z.id AS prize_id, z.name AS prize, z.value, p.id AS project_id, p.title, tm.name AS team_name,"
        " u.id AS user_id, u.name FROM prize_awards a JOIN prizes z ON z.id = a.prize_id"
        " JOIN projects p ON p.id = a.project_id JOIN teams tm ON tm.id = p.team_id"
        " JOIN team_members m ON m.team_id = tm.id JOIN users u ON u.id = m.user_id WHERE z.event_id = ?",
        (event.id,),
    )
    if awards and not event.results_published:
        raise Conflict("Publish results before issuing award certificates.")
    issued = 0
    with transaction(db):
        for m in members:
            body = {**_base(settings, "participation", event), "person": {"id": m["id"], "name": m["name"]},
                    "team": m["team_name"], "project": {"id": m["project_id"], "title": m["title"]}}
            if _issue(db, settings, "participation", event, m["id"], m["name"],
                      f"participation:{event.id}:{m['id']}", body):
                issued += 1
        for a in awards:
            body = {**_base(settings, "award", event), "person": {"id": a["user_id"], "name": a["name"]},
                    "team": a["team_name"], "project": {"id": a["project_id"], "title": a["title"]},
                    "prize": {"id": a["prize_id"], "name": a["prize"], "value": a["value"]}}
            if _issue(db, settings, "award", event, a["user_id"], a["name"],
                      f"award:{event.id}:{a['user_id']}:{a['prize_id']}", body):
                issued += 1
        audit.record(db, "records.issue", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"count": issued, "kind": "certificates"})
    return issued


def award_prize(db: sqlite3.Connection, actor: User, event: Event, prize_id: str, project_id: str | None) -> None:
    policy.require_organizer(db, actor, event.id)
    prize = fetch_one(db, "SELECT * FROM prizes WHERE id = ? AND event_id = ?", (prize_id, event.id))
    if prize is None:
        raise NotFound("No such prize.")
    with transaction(db):
        db.execute("DELETE FROM prize_awards WHERE prize_id = ?", (prize_id,))
        if project_id:
            project = fetch_one(db, "SELECT title FROM projects WHERE id = ? AND event_id = ? AND status = 'submitted'",
                                (project_id, event.id))
            if project is None:
                raise ValidationFailed(fields={"project": "Pick a submitted project from this event."})
            db.execute("INSERT INTO prize_awards VALUES (?, ?, ?, ?)", (prize_id, project_id, actor.id, clock.now_iso()))
            audit.record(db, "prize.award", actor=actor, event_id=event.id, target_type="prize", target_id=prize_id,
                         detail={"prize": prize["name"], "title": project["title"]})


def prize_board(db: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(db, "SELECT z.*, t.name AS track_name, a.project_id, p.title AS winner FROM prizes z"
                         " LEFT JOIN tracks t ON t.id = z.track_id LEFT JOIN prize_awards a ON a.prize_id = z.id"
                         " LEFT JOIN projects p ON p.id = a.project_id WHERE z.event_id = ? ORDER BY z.position", (event_id,))
    return [dict(r) for r in rows]


# -------------------------------------------------------------- reading

@dataclass
class Verification:
    record: sqlite3.Row | None
    payload: dict[str, Any] | None
    valid_signature: bool
    revoked: bool
    reason: str

    @property
    def ok(self) -> bool:
        return self.valid_signature and not self.revoked


def get_record(db: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    row = fetch_one(db, "SELECT * FROM records WHERE id = ?", (record_id,))
    if row is None:
        raise NotFound("No such record.")
    return row


def verify_record(db: sqlite3.Connection, settings: Settings, record_id: str) -> Verification:
    row = fetch_one(db, "SELECT * FROM records WHERE id = ?", (record_id,))
    if row is None:
        return Verification(None, None, False, False, "No record with that id was issued here.")
    payload = json.loads(row["payload"])
    return verify_payload(settings, payload, row["signature"], row)


def verify_payload(settings: Settings, payload: dict[str, Any], signature: str, row=None) -> Verification:
    s = signer(settings)
    if payload.get("key_id") and payload["key_id"] != s.key_id:
        return Verification(row, payload, False, False, "Signed with a different key than this portal's current key.")
    valid = signing.verify(s.public_key, payload, signature)
    revoked = bool(row and row["revoked_at"])
    if not valid:
        reason = "The signature does not match: the record was altered or was not issued by this portal."
    elif revoked:
        reason = f"Genuine, but revoked ({row['revoked_reason'] or 'no reason given'})."
    else:
        reason = "Genuine: signed by this portal's key and not revoked."
    return Verification(row, payload, valid, revoked, reason)


def export(row: sqlite3.Row, settings: Settings) -> dict[str, Any]:
    return {"payload": json.loads(row["payload"]), "signature": row["signature"], "key_id": row["key_id"],
            "algorithm": "Ed25519", "public_key_url": f"{settings.base_url}/.well-known/doggfather/signing-key.pem",
            "revoked_at": row["revoked_at"]}


def list_records(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT * FROM records WHERE event_id = ? ORDER BY kind, subject_name, issued_at DESC", (event_id,))


def records_for_user(db: sqlite3.Connection, user: User) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT r.*, e.name AS event_name FROM records r JOIN events e ON e.id = r.event_id"
                         " WHERE r.subject_user_id = ? AND r.revoked_at IS NULL ORDER BY r.issued_at DESC", (user.id,))


def reveal(db: sqlite3.Connection, actor: User | None, record_id: str) -> dict[str, Any]:
    """Disclose the scorecards behind a judge record's commitment. Only the
    judge themself or the event's organizers may do this."""
    row = get_record(db, record_id)
    if row["kind"] != "judge_record":
        raise ValidationFailed("Only judge records carry a score commitment.")
    if actor is None or not policy.can_view_judge_scores(db, actor, row["subject_user_id"], row["event_id"]):
        raise Forbidden("Only the judge or the event's organizers can reveal these scores.")
    cards = scoring.scores_for_judge(db, row["subject_user_id"], row["event_id"])
    payload = json.loads(row["payload"])
    current = scores_commitment(cards)
    return {"scorecards": [{"project_id": c["project_id"], "criteria": c["criteria"]} for c in cards],
            "committed_sha256": payload["scores_sha256"], "current_sha256": current,
            "matches": current == payload["scores_sha256"]}


def revoke(db: sqlite3.Connection, actor: User, record_id: str, reason: str) -> None:
    row = get_record(db, record_id)
    policy.require_organizer(db, actor, row["event_id"])
    if row["revoked_at"]:
        return
    reason = (reason or "revoked by the organizers").strip()[:200]
    with transaction(db):
        db.execute("UPDATE records SET revoked_at = ?, revoked_reason = ? WHERE id = ?", (clock.now_iso(), reason, record_id))
        audit.record(db, "records.revoke", actor=actor, event_id=row["event_id"], target_type="record",
                     target_id=record_id, detail={"record": record_id, "name": row["subject_name"], "reason": reason})
