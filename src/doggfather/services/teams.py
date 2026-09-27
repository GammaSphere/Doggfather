"""Team formation by invite link.

Rules
- One team per person per event (also a unique index in the schema).
- Judges and organizers of an event cannot join its teams.
- Team size is capped by the event.
- Teams lock when submissions close: no joining, leaving or removing after
  the deadline, so a finished project's credits cannot be rewritten.
"""

from __future__ import annotations

import sqlite3

from .. import audit, clock
from ..auth import User
from ..db import fetch_all, fetch_one, fetch_value, new_id, transaction
from ..errors import Conflict, Forbidden, NotFound, SubmissionsClosed, ValidationFailed
from ..security import new_token
from . import webhooks
from .events import Event, get_event


def get_team(db: sqlite3.Connection, team_id: str) -> sqlite3.Row:
    row = fetch_one(db, "SELECT * FROM teams WHERE id = ?", (team_id,))
    if row is None:
        raise NotFound("No such team.")
    return row


def get_team_by_code(db: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = fetch_one(db, "SELECT * FROM teams WHERE invite_code = ?", (code,))
    if row is None:
        raise NotFound("That invite link is not valid. Ask your captain for a fresh one.")
    return row


def team_for_user(db: sqlite3.Connection, user_id: str, event_id: str) -> sqlite3.Row | None:
    return fetch_one(
        db,
        "SELECT t.*, m.role AS my_role FROM teams t JOIN team_members m ON m.team_id = t.id"
        " WHERE m.user_id = ? AND m.event_id = ?",
        (user_id, event_id),
    )


def members(db: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT u.id, u.name, u.email, m.role, m.joined_at FROM team_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.team_id = ? ORDER BY m.role = 'captain' DESC, m.joined_at, u.name",
        (team_id,),
    )


def is_member(db: sqlite3.Connection, user_id: str, team_id: str) -> bool:
    return fetch_one(db, "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id)) is not None


def _assert_can_compete(db: sqlite3.Connection, actor: User, event: Event) -> None:
    staff = fetch_one(
        db, "SELECT role FROM event_members WHERE event_id = ? AND user_id = ? AND role IN ('judge', 'organizer')",
        (event.id, actor.id),
    )
    if staff:
        raise Forbidden(f"You are a {staff['role']} for this event, so you cannot join a team in it.",
                        code="conflict_of_interest")


def _assert_teams_open(event: Event) -> None:
    if event.submissions_closed():
        raise SubmissionsClosed("Teams locked when submissions closed.", closed_at=event.submissions_close_at)


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not 2 <= len(name) <= 60:
        raise ValidationFailed(fields={"team_name": "Team names are 2 to 60 characters."})
    return name


def create_team(db: sqlite3.Connection, actor: User, event: Event, name: str) -> str:
    _assert_teams_open(event)
    _assert_can_compete(db, actor, event)
    name = _clean_name(name)
    if team_for_user(db, actor.id, event.id):
        raise Conflict("You are already on a team for this event.")
    team_id = new_id("tm")
    now = clock.now_iso()
    with transaction(db):
        db.execute("INSERT INTO teams (id, event_id, name, invite_code, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (team_id, event.id, name, new_token(9), actor.id, now))
        db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'participant', ?)", (event.id, actor.id, now))
        db.execute("INSERT INTO team_members (team_id, event_id, user_id, role, joined_at) VALUES (?, ?, ?, 'captain', ?)",
                   (team_id, event.id, actor.id, now))
        audit.record(db, "team.create", actor=actor, event_id=event.id, target_type="team", target_id=team_id,
                     detail={"name": name})
        webhooks.emit(db, event.id, "team.created", {"team_id": team_id, "name": name})
    return team_id


def join_team(db: sqlite3.Connection, actor: User, code: str) -> sqlite3.Row:
    team = get_team_by_code(db, code)
    event = get_event(db, team["event_id"])
    if is_member(db, actor.id, team["id"]):
        return team
    _assert_teams_open(event)
    _assert_can_compete(db, actor, event)
    if team_for_user(db, actor.id, event.id):
        raise Conflict("You are on another team for this event. Leave it first.")
    with transaction(db):
        size = fetch_value(db, "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],))
        if size >= event.max_team_size:
            raise Conflict(f"This team is full ({event.max_team_size} people).", code="team_full")
        now = clock.now_iso()
        db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'participant', ?)", (event.id, actor.id, now))
        db.execute("INSERT INTO team_members (team_id, event_id, user_id, role, joined_at) VALUES (?, ?, ?, 'member', ?)",
                   (team["id"], event.id, actor.id, now))
        audit.record(db, "team.join", actor=actor, event_id=event.id, target_type="team", target_id=team["id"],
                     detail={"name": team["name"]})
    return team


def _captain_only(db: sqlite3.Connection, actor: User, team: sqlite3.Row) -> None:
    if not fetch_one(db, "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ? AND role = 'captain'",
                     (team["id"], actor.id)):
        raise Forbidden("Only the team captain can do that.")


def _drop_member(db: sqlite3.Connection, event_id: str, team_id: str, user_id: str) -> None:
    db.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id))
    db.execute("DELETE FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'participant'", (event_id, user_id))


def leave_team(db: sqlite3.Connection, actor: User, team_id: str) -> None:
    team = get_team(db, team_id)
    event = get_event(db, team["event_id"])
    if not is_member(db, actor.id, team_id):
        raise Forbidden("You are not on this team.")
    _assert_teams_open(event)
    with transaction(db):
        others = [m for m in members(db, team_id) if m["id"] != actor.id]
        if not others and fetch_one(db, "SELECT 1 FROM projects WHERE team_id = ? AND status = 'submitted'", (team_id,)):
            raise Conflict("You are the last member of a team with a submitted project. Withdraw it first.")
        was_captain = fetch_value(db, "SELECT role FROM team_members WHERE team_id = ? AND user_id = ?",
                                  (team_id, actor.id)) == "captain"
        _drop_member(db, event.id, team_id, actor.id)
        if not others:
            db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        elif was_captain:
            db.execute("UPDATE team_members SET role = 'captain' WHERE team_id = ? AND user_id = ?",
                       (team_id, others[0]["id"]))
        audit.record(db, "team.leave", actor=actor, event_id=event.id, target_type="team", target_id=team_id,
                     detail={"name": team["name"]})


def remove_member(db: sqlite3.Connection, actor: User, team_id: str, user_id: str) -> None:
    team = get_team(db, team_id)
    event = get_event(db, team["event_id"])
    _captain_only(db, actor, team)
    _assert_teams_open(event)
    if user_id == actor.id:
        raise Conflict("Use 'leave team' to remove yourself.")
    member = fetch_one(db, "SELECT u.name FROM team_members m JOIN users u ON u.id = m.user_id"
                           " WHERE m.team_id = ? AND m.user_id = ?", (team_id, user_id))
    if member is None:
        raise NotFound("That person is not on this team.")
    with transaction(db):
        _drop_member(db, event.id, team_id, user_id)
        audit.record(db, "team.remove_member", actor=actor, event_id=event.id, target_type="team", target_id=team_id,
                     detail={"name": team["name"], "member": member["name"]})


def regenerate_invite(db: sqlite3.Connection, actor: User, team_id: str) -> str:
    team = get_team(db, team_id)
    _captain_only(db, actor, team)
    code = new_token(9)
    with transaction(db):
        db.execute("UPDATE teams SET invite_code = ? WHERE id = ?", (code, team_id))
        audit.record(db, "team.invite_reset", actor=actor, event_id=team["event_id"], target_type="team",
                     target_id=team_id, detail={"name": team["name"]})
    return code


def rename_team(db: sqlite3.Connection, actor: User, team_id: str, name: str) -> None:
    team = get_team(db, team_id)
    _captain_only(db, actor, team)
    _assert_teams_open(get_event(db, team["event_id"]))
    db.execute("UPDATE teams SET name = ? WHERE id = ?", (_clean_name(name), team_id))
