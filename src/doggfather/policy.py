"""Authorization: the one place that decides who may see or change what.

HTML pages and JSON endpoints both call these functions, and the
judge-scoped queries in the services filter on the judge id in SQL, so a
check can never be "painted on" in a template and forgotten in the API.

Role isolation matrix (enforced here, tested in tests/test_isolation.py):

    actor        own scores  peer scores  other tracks  aggregates  audit log
    visitor          -           -            -             -           -
    participant      -           -            -             -           -
    judge            +           -            -             -           -
    organizer        +           +            +             +           +
    admin            +           +            +             +           +

"Aggregates" become public only when an organizer publishes results.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .auth import User
from .db import fetch_all, fetch_one
from .errors import Forbidden, NotAuthenticated

ROLES = ("visitor", "participant", "judge", "organizer", "admin")


def roles_in_event(db: sqlite3.Connection, user: User | None, event_id: str) -> frozenset[str]:
    if user is None:
        return frozenset({"visitor"})
    rows = fetch_all(db, "SELECT role FROM event_members WHERE event_id = ? AND user_id = ?", (event_id, user.id))
    roles = {row["role"] for row in rows}
    if user.is_admin:
        roles.add("admin")
    return frozenset(roles)


def is_organizer(db: sqlite3.Connection, user: User | None, event_id: str) -> bool:
    if user is None:
        return False
    if user.is_admin:
        return True
    return fetch_one(db, "SELECT 1 FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'organizer'",
                     (event_id, user.id)) is not None


def is_judge(db: sqlite3.Connection, user: User | None, event_id: str) -> bool:
    if user is None:
        return False
    return fetch_one(db, "SELECT 1 FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'judge'",
                     (event_id, user.id)) is not None


def is_judge_anywhere(db: sqlite3.Connection, user: User | None) -> bool:
    if user is None:
        return False
    return fetch_one(db, "SELECT 1 FROM event_members WHERE user_id = ? AND role = 'judge'", (user.id,)) is not None


def _require_login(user: User | None) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_organizer(db: sqlite3.Connection, user: User | None, event_id: str) -> User:
    current = _require_login(user)
    if not is_organizer(db, current, event_id):
        raise Forbidden("Only this event's organizers can do that.")
    return current


def require_judge(db: sqlite3.Connection, user: User | None, event_id: str) -> User:
    current = _require_login(user)
    if not is_judge(db, current, event_id):
        raise Forbidden("You are not a judge for this event.")
    return current


def require_admin(user: User | None) -> User:
    current = _require_login(user)
    if not current.is_admin:
        raise Forbidden("Admins only.")
    return current


def judge_track_ids(db: sqlite3.Connection, judge_id: str, event_id: str) -> set[str]:
    rows = fetch_all(db, "SELECT track_id FROM judge_tracks WHERE user_id = ? AND event_id = ?", (judge_id, event_id))
    return {row["track_id"] for row in rows}


def judge_can_see_project(db: sqlite3.Connection, judge_id: str, project: Any) -> bool:
    """A judge sees a project in judging views only if it sits in one of
    their tracks, or an organizer explicitly assigned it to them."""
    if project["track_id"] and project["track_id"] in judge_track_ids(db, judge_id, project["event_id"]):
        return True
    return fetch_one(db, "SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?",
                     (judge_id, project["id"])) is not None


def can_view_judge_scores(db: sqlite3.Connection, actor: User | None, judge_id: str,
                          event_id: str | None = None) -> bool:
    """Own scores: yes. Anyone else's: only organizers of an event the judge
    serves (or admins). Peer judges and participants: never."""
    if actor is None:
        return False
    if actor.id == judge_id or actor.is_admin:
        return True
    if event_id is not None:
        return is_organizer(db, actor, event_id)
    row = fetch_one(
        db,
        "SELECT 1 FROM event_members o JOIN event_members j ON j.event_id = o.event_id"
        " WHERE o.user_id = ? AND o.role = 'organizer' AND j.user_id = ? AND j.role = 'judge'",
        (actor.id, judge_id),
    )
    return row is not None


def organizer_event_ids(db: sqlite3.Connection, user: User) -> set[str]:
    if user.is_admin:
        return {row["id"] for row in fetch_all(db, "SELECT id FROM events")}
    rows = fetch_all(db, "SELECT event_id FROM event_members WHERE user_id = ? AND role = 'organizer'", (user.id,))
    return {row["event_id"] for row in rows}
