"""Community voting: quadratic, gated, randomized, hidden until published.

Quadratic voting
    Each voter gets C credits (event setting, default 25). Putting v votes on
    one project costs v^2 credits. Enthusiasm is still expressible, but
    concentrating a ballot is expensive: 5 votes on one project uses the whole
    default budget, while 1 vote on each of 25 projects costs the same. The
    budget is checked in a write transaction here and enforced again by
    database triggers.

Gates (event.voting_mode)
    account  one ballot per logged-in account
    email    one ballot per verified address; a 6-digit code goes to the
             outbox. Addresses are normalized (case, +tags, dots in Gmail) so
             ``a.b+1@gmail.com`` and ``ab@gmail.com`` are one voter
    link     one ballot per device cookie; the weakest gate, so IP and device
             clustering are flagged for organizer review

Order
    Each voter sees the projects in their own stable shuffle, seeded by an
    HMAC of their identity, so top-of-page position bias averages out across
    voters and a reload does not reshuffle.

Visibility
    Tallies are organizer-only while voting runs. The public sees them only
    after voting closes and results are published.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from math import isqrt
from typing import Any

from .. import audit, clock, policy
from ..auth import User, validate_email
from ..config import Settings
from ..db import fetch_all, fetch_one, fetch_value, new_id, transaction
from ..errors import Conflict, Forbidden, NotAuthenticated, NotFound, ResultsHidden, ValidationFailed
from ..security import keyed_hash, token_hash
from . import mailer, webhooks
from .events import Event

CODE_TTL = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5
IP_CLUSTER_THRESHOLD = 5
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


@dataclass(frozen=True)
class Voter:
    id: str
    event_id: str
    kind: str
    user_id: str | None
    email: str | None
    voided: bool


def normalize_voter_email(email: str) -> str:
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if domain else email


def _voter(row: sqlite3.Row | None) -> Voter | None:
    if row is None:
        return None
    return Voter(row["id"], row["event_id"], row["kind"], row["user_id"], row["email"], row["voided_at"] is not None)


def get_voter(db: sqlite3.Connection, voter_id: str) -> Voter | None:
    return _voter(fetch_one(db, "SELECT * FROM voters WHERE id = ?", (voter_id,)))


def assert_open(event: Event) -> None:
    if not event.voting_enabled:
        raise NotFound("This event has no community vote.")
    if not event.voting_open():
        if event.voting_closed():
            raise Forbidden("Community voting has closed.", code="voting_closed")
        raise Forbidden("Community voting has not opened yet.", code="voting_not_open", opens_at=event.voting_open_at)


# ------------------------------------------------------------ identities

def voter_for_account(db: sqlite3.Connection, event: Event, user: User | None, ip_hash: str) -> Voter:
    if user is None:
        raise NotAuthenticated("Log in to vote.")
    row = fetch_one(db, "SELECT * FROM voters WHERE event_id = ? AND user_id = ?", (event.id, user.id))
    if row:
        return _voter(row)  # type: ignore[return-value]
    voter_id = new_id("vtr")
    db.execute("INSERT INTO voters (id, event_id, kind, user_id, email, ip_hash, created_at, verified_at)"
               " VALUES (?, ?, 'account', ?, ?, ?, ?, ?)",
               (voter_id, event.id, user.id, normalize_voter_email(user.email), ip_hash, clock.now_iso(), clock.now_iso()))
    return get_voter(db, voter_id)  # type: ignore[return-value]


def voter_for_device(db: sqlite3.Connection, settings: Settings, event: Event, device_token: str, ip_hash: str,
                     ua_hash: str) -> Voter:
    device_hash = keyed_hash(settings.secret_key, device_token, "vote-device")
    row = fetch_one(db, "SELECT * FROM voters WHERE event_id = ? AND device_hash = ?", (event.id, device_hash))
    if row:
        return _voter(row)  # type: ignore[return-value]
    voter_id = new_id("vtr")
    with transaction(db):
        db.execute("INSERT INTO voters (id, event_id, kind, device_hash, ip_hash, ua_hash, created_at)"
                   " VALUES (?, ?, 'link', ?, ?, ?, ?)", (voter_id, event.id, device_hash, ip_hash, ua_hash, clock.now_iso()))
        _flag_ip_cluster(db, event, voter_id, ip_hash)
    return get_voter(db, voter_id)  # type: ignore[return-value]


def voter_for_verified_email(db: sqlite3.Connection, event: Event, email: str, ip_hash: str) -> Voter:
    """A logged-in account whose address is already verified skips the code."""
    normalized = normalize_voter_email(email)
    row = fetch_one(db, "SELECT * FROM voters WHERE event_id = ? AND email = ?", (event.id, normalized))
    if row:
        return _voter(row)  # type: ignore[return-value]
    voter_id = new_id("vtr")
    db.execute("INSERT INTO voters (id, event_id, kind, email, ip_hash, created_at, verified_at)"
               " VALUES (?, ?, 'email', ?, ?, ?, ?)", (voter_id, event.id, normalized, ip_hash, clock.now_iso(), clock.now_iso()))
    return get_voter(db, voter_id)  # type: ignore[return-value]


def request_code(db: sqlite3.Connection, settings: Settings, event: Event, email: str) -> None:
    assert_open(event)
    error = validate_email((email or "").strip().lower())
    if error:
        raise ValidationFailed(fields={"email": error})
    normalized = normalize_voter_email(email)
    code = f"{secrets.randbelow(10**6):06d}"
    now = clock.now()
    with transaction(db):
        db.execute("UPDATE vote_codes SET used_at = ? WHERE event_id = ? AND email = ? AND used_at IS NULL",
                   (clock.iso(now), event.id, normalized))
        db.execute("INSERT INTO vote_codes (event_id, email, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                   (event.id, normalized, token_hash(f"{event.id}:{normalized}:{code}"), clock.iso(now), clock.iso(now + CODE_TTL)))
        mailer.send(db, settings, email.strip(), f"Your voting code for {event.name}",
                    f"Your code is {code}\n\nIt works for 15 minutes. If you did not ask for it, ignore this message.")


def verify_code(db: sqlite3.Connection, event: Event, email: str, code: str, ip_hash: str) -> Voter:
    assert_open(event)
    normalized = normalize_voter_email(email)
    with transaction(db):
        row = fetch_one(db, "SELECT * FROM vote_codes WHERE event_id = ? AND email = ? AND used_at IS NULL"
                            " ORDER BY id DESC LIMIT 1", (event.id, normalized))
        if row is None or row["expires_at"] <= clock.now_iso() or row["attempts"] >= MAX_CODE_ATTEMPTS:
            raise Forbidden("That code has expired. Request a new one.", code="code_expired")
        expected = token_hash(f"{event.id}:{normalized}:{(code or '').strip()}")
        if not hmac.compare_digest(expected, row["code_hash"]):
            db.execute("UPDATE vote_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            raise ValidationFailed(fields={"code": "That code is not right."})
        db.execute("UPDATE vote_codes SET used_at = ? WHERE id = ?", (clock.now_iso(), row["id"]))
        existing = fetch_one(db, "SELECT * FROM voters WHERE event_id = ? AND email = ?", (event.id, normalized))
        if existing:
            return _voter(existing)  # type: ignore[return-value]
        voter_id = new_id("vtr")
        db.execute("INSERT INTO voters (id, event_id, kind, email, ip_hash, created_at, verified_at)"
                   " VALUES (?, ?, 'email', ?, ?, ?, ?)", (voter_id, event.id, normalized, ip_hash, clock.now_iso(), clock.now_iso()))
        audit.record(db, "vote.voter_verified", event_id=event.id, target_type="voter", target_id=voter_id,
                     detail={"email": normalized})
        _flag_ip_cluster(db, event, voter_id, ip_hash)
    return get_voter(db, voter_id)  # type: ignore[return-value]


def _flag_ip_cluster(db: sqlite3.Connection, event: Event, voter_id: str, ip_hash: str) -> None:
    peers = fetch_value(db, "SELECT COUNT(*) FROM voters WHERE event_id = ? AND ip_hash = ?", (event.id, ip_hash), default=0)
    if peers >= IP_CLUSTER_THRESHOLD:
        reason = f"{peers} voters from one network"
        db.execute("UPDATE voters SET flagged = ? WHERE event_id = ? AND ip_hash = ? AND flagged IS NULL",
                   (reason, event.id, ip_hash))
        audit.record(db, "vote.flag", event_id=event.id, target_type="voter", target_id=voter_id,
                     detail={"voter": voter_id, "reason": reason})


# ---------------------------------------------------------------- ballots

def ballot_order(secret: str, seed_key: str, event_id: str, project_ids: list[str]) -> list[str]:
    """A stable per-voter shuffle."""
    def key(pid: str) -> str:
        return hmac.new(secret.encode(), f"ballot:{event_id}:{seed_key}:{pid}".encode(), hashlib.sha256).hexdigest()

    return sorted(project_ids, key=key)


def own_project_ids(db: sqlite3.Connection, event: Event, voter: Voter | None) -> set[str]:
    """Projects this voter may not vote for: their own team's."""
    if voter is None:
        return set()
    clauses, params = [], []
    if voter.user_id:
        clauses.append("m.user_id = ?")
        params.append(voter.user_id)
    if voter.email:
        clauses.append("LOWER(u.email) = ?")
        params.append(voter.email)
    if not clauses:
        return set()
    rows = fetch_all(db, "SELECT p.id FROM projects p JOIN team_members m ON m.team_id = p.team_id"
                         f" JOIN users u ON u.id = m.user_id WHERE p.event_id = ? AND ({' OR '.join(clauses)})",
                         (event.id, *params))
    return {r["id"] for r in rows}


def current_ballot(db: sqlite3.Connection, voter: Voter | None) -> dict[str, int]:
    if voter is None:
        return {}
    return {r["project_id"]: r["votes"] for r in fetch_all(db, "SELECT project_id, votes FROM ballot_items WHERE voter_id = ?",
                                                             (voter.id,))}


def ballot(db: sqlite3.Connection, settings: Settings, event: Event, voter: Voter | None, seed_key: str) -> dict[str, Any]:
    projects = fetch_all(
        db,
        "SELECT p.id, p.title, p.tagline, p.thumbnail, t.name AS track_name, tm.name AS team_name FROM projects p"
        " JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id"
        " WHERE p.event_id = ? AND p.status = 'submitted'",
        (event.id,),
    )
    by_id = {p["id"]: dict(p) for p in projects}
    order = ballot_order(settings.secret_key, seed_key, event.id, list(by_id))
    mine = own_project_ids(db, event, voter)
    votes = current_ballot(db, voter)
    return {
        "items": [{**by_id[pid], "votes": votes.get(pid, 0), "own": pid in mine} for pid in order],
        "credits": event.vote_credits,
        "spent": sum(v * v for v in votes.values()),
        "max_votes": isqrt(event.vote_credits),
    }


def cast_ballot(db: sqlite3.Connection, event: Event, voter: Voter, allocation: dict[str, int]) -> dict[str, int]:
    """Replace the voter's ballot with ``allocation`` (project -> votes)."""
    assert_open(event)
    if voter.voided:
        raise Forbidden("This ballot was voided by the organizers.", code="voter_voided")
    clean: dict[str, int] = {}
    for pid, raw in allocation.items():
        try:
            votes = int(raw)
        except (TypeError, ValueError):
            raise ValidationFailed(fields={pid: "Votes are whole numbers."}) from None
        if votes < 0:
            raise ValidationFailed(fields={pid: "Votes cannot be negative."})
        if votes:
            clean[pid] = votes
    cost = sum(v * v for v in clean.values())
    if cost > event.vote_credits:
        raise ValidationFailed(f"That ballot costs {cost} credits; you have {event.vote_credits}.",
                               fields={"ballot": "over budget"}, cost=cost, credits=event.vote_credits)
    if clean:
        valid = {r["id"] for r in fetch_all(db, "SELECT id FROM projects WHERE event_id = ? AND status = 'submitted'",
                                            (event.id,))}
        unknown = set(clean) - valid
        if unknown:
            raise ValidationFailed(fields={pid: "Not on this ballot." for pid in unknown})
        own = own_project_ids(db, event, voter) & set(clean)
        if own:
            raise Forbidden("You cannot vote for your own team's project.", code="own_project")
    now = clock.now_iso()
    with transaction(db):
        db.execute("DELETE FROM ballot_items WHERE voter_id = ?", (voter.id,))
        db.executemany("INSERT INTO ballot_items (voter_id, project_id, votes, updated_at) VALUES (?, ?, ?, ?)",
                       [(voter.id, pid, votes, now) for pid, votes in clean.items()])
        audit.record(db, "vote.ballot", event_id=event.id, target_type="voter", target_id=voter.id,
                     detail={"votes": sum(clean.values()), "projects": len(clean), "credits": cost})
        webhooks.emit(db, event.id, "vote.cast", {"projects": len(clean), "votes": sum(clean.values())})
    return clean


# ---------------------------------------------------------------- tallies

def tally(db: sqlite3.Connection, event: Event) -> list[dict[str, Any]]:
    rows = fetch_all(
        db,
        "SELECT p.id, p.title, tm.name AS team_name, COALESCE(SUM(b.votes), 0) AS votes, COUNT(b.voter_id) AS voters"
        " FROM projects p JOIN teams tm ON tm.id = p.team_id"
        " LEFT JOIN ballot_items b ON b.project_id = p.id"
        " AND b.voter_id IN (SELECT id FROM voters WHERE event_id = p.event_id AND voided_at IS NULL)"
        " WHERE p.event_id = ? AND p.status = 'submitted' GROUP BY p.id ORDER BY votes DESC, voters DESC, p.title",
        (event.id,),
    )
    out, rank, last = [], 0, None
    for position, row in enumerate(rows, start=1):
        if row["votes"] != last:
            rank, last = position, row["votes"]
        out.append({**dict(row), "rank": rank})
    return out


def visible_tally(db: sqlite3.Connection, actor: User | None, event: Event) -> list[dict[str, Any]]:
    if policy.is_organizer(db, actor, event.id):
        return tally(db, event)
    if not event.voting_enabled:
        raise NotFound("This event has no community vote.")
    if not event.tally_public:
        raise ResultsHidden("Community votes stay hidden until voting closes and results are published.")
    return tally(db, event)


def voters_overview(db: sqlite3.Connection, actor: User, event: Event) -> list[sqlite3.Row]:
    policy.require_organizer(db, actor, event.id)
    return fetch_all(
        db,
        "SELECT v.*, (SELECT COALESCE(SUM(votes), 0) FROM ballot_items WHERE voter_id = v.id) AS votes,"
        " (SELECT COALESCE(SUM(votes * votes), 0) FROM ballot_items WHERE voter_id = v.id) AS spent"
        " FROM voters v WHERE v.event_id = ? ORDER BY v.flagged IS NULL, v.created_at DESC",
        (event.id,),
    )


def set_voided(db: sqlite3.Connection, actor: User, event: Event, voter_id: str, voided: bool) -> None:
    policy.require_organizer(db, actor, event.id)
    voter = get_voter(db, voter_id)
    if voter is None or voter.event_id != event.id:
        raise NotFound("No such voter.")
    if voter.voided == voided:
        raise Conflict("Nothing to change.")
    with transaction(db):
        db.execute("UPDATE voters SET voided_at = ?, voided_by = ? WHERE id = ?",
                   (clock.now_iso() if voided else None, actor.id if voided else None, voter_id))
        audit.record(db, "vote.void" if voided else "vote.restore", actor=actor, event_id=event.id,
                     target_type="voter", target_id=voter_id, detail={"voter": voter_id})
