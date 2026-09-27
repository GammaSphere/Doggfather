"""Judge-side scoring.

Isolation rule: every function here that returns scores takes a ``judge_id``
and puts it in the SQL ``WHERE`` clause. There is no "fetch everything and
filter in Python" path that a caller could forget to filter. Organizer-wide
reads live in ``results.py`` and are reachable only through the organizer
policy check.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import audit, clock, policy
from ..auth import User
from ..db import fetch_all, fetch_one, new_id, placeholders, transaction
from ..errors import Forbidden, NotFound, ValidationFailed
from . import rubric, webhooks
from .events import get_event
from .projects import get_project


def judge_events(db: sqlite3.Connection, judge_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        db,
        "SELECT e.*, (SELECT COUNT(*) FROM assignments a WHERE a.event_id = e.id AND a.judge_id = m.user_id) AS assigned,"
        " (SELECT COUNT(*) FROM assignments a WHERE a.event_id = e.id AND a.judge_id = m.user_id AND a.status = 'done') AS done"
        " FROM event_members m JOIN events e ON e.id = m.event_id WHERE m.user_id = ? AND m.role = 'judge'"
        " ORDER BY e.judging_close_at DESC",
        (judge_id,),
    )
    return [dict(r) for r in rows]


def queue(db: sqlite3.Connection, judge_id: str, event_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        db,
        "SELECT a.id AS assignment_id, a.status, a.batch, p.id, p.title, p.tagline, t.name AS track_name,"
        " s.updated_at AS scored_at FROM assignments a JOIN projects p ON p.id = a.project_id"
        " LEFT JOIN tracks t ON t.id = p.track_id LEFT JOIN scores s ON s.assignment_id = a.id"
        " WHERE a.judge_id = ? AND a.event_id = ? ORDER BY a.status = 'done', t.position, p.title",
        (judge_id, event_id),
    )
    return [dict(r) for r in rows]


def assignment_for(db: sqlite3.Connection, judge_id: str, project_id: str) -> sqlite3.Row | None:
    return fetch_one(db, "SELECT * FROM assignments WHERE judge_id = ? AND project_id = ?", (judge_id, project_id))


def next_pending(db: sqlite3.Connection, judge_id: str, event_id: str, after: str | None = None) -> str | None:
    items = [q for q in queue(db, judge_id, event_id) if q["status"] == "pending" and q["id"] != after]
    return items[0]["id"] if items else None


def own_score(db: sqlite3.Connection, judge_id: str, project_id: str) -> dict[str, Any] | None:
    row = fetch_one(db, "SELECT * FROM scores WHERE judge_id = ? AND project_id = ?", (judge_id, project_id))
    if row is None:
        return None
    items = {r["criterion_id"]: r["value"] for r in fetch_all(db, "SELECT * FROM score_items WHERE score_id = ?", (row["id"],))}
    return {**dict(row), "items": items}


def require_scoring_access(db: sqlite3.Connection, judge: User, project: sqlite3.Row) -> sqlite3.Row:
    """The judge must hold an assignment for this project, and the project
    must be in one of their tracks (unless an organizer assigned it by hand)."""
    policy.require_judge(db, judge, project["event_id"])
    assignment = assignment_for(db, judge.id, project["id"])
    if assignment is None or not policy.judge_can_see_project(db, judge.id, project):
        raise Forbidden("This project is not in your judging queue.", code="not_assigned")
    return assignment


def save_score(db: sqlite3.Connection, judge: User, project_id: str, values: dict[str, Any], comment: str = "") -> str:
    project = get_project(db, project_id)
    event = get_event(db, project["event_id"])
    assignment = require_scoring_access(db, judge, project)
    if not event.judging_open():
        message = ("Judging opens when submissions close." if not event.submissions_closed()
                   else "Judging for this event is closed.")
        raise Forbidden(message, code="judging_closed")

    criteria = rubric.list_criteria(db, event.id)
    by_key = {c["key"]: c for c in criteria}
    by_id = {c["id"]: c for c in criteria}
    clean: dict[str, int] = {}
    errors: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        criterion = by_id.get(raw_key) or by_key.get(raw_key)
        if criterion is None:
            errors[raw_key] = "Not part of this rubric."
            continue
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            errors[criterion["key"]] = "Pick a score."
            continue
        if not criterion["min_score"] <= value <= criterion["max_score"]:
            errors[criterion["key"]] = f"Between {criterion['min_score']} and {criterion['max_score']}."
            continue
        clean[criterion["id"]] = value
    for criterion in criteria:
        if criterion["id"] not in clean and criterion["key"] not in errors:
            errors[criterion["key"]] = "Score every criterion."
    comment = (comment or "").strip()
    if len(comment) > 4000:
        errors["comment"] = "Keep comments under 4000 characters."
    if errors:
        raise ValidationFailed(fields=errors)

    now = clock.now_iso()
    with transaction(db):
        existing = fetch_one(db, "SELECT id FROM scores WHERE assignment_id = ?", (assignment["id"],))
        if existing:
            score_id = existing["id"]
            db.execute("UPDATE scores SET comment = ?, updated_at = ? WHERE id = ?", (comment, now, score_id))
        else:
            score_id = new_id("scr")
            db.execute("INSERT INTO scores (id, assignment_id, event_id, judge_id, project_id, comment, submitted_at, updated_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (score_id, assignment["id"], event.id, judge.id, project_id,
                                                            comment, now, now))
        db.executemany("INSERT INTO score_items (score_id, criterion_id, value) VALUES (?, ?, ?)"
                       " ON CONFLICT (score_id, criterion_id) DO UPDATE SET value = excluded.value",
                       [(score_id, cid, v) for cid, v in clean.items()])
        db.execute("UPDATE assignments SET status = 'done' WHERE id = ?", (assignment["id"],))
        audit.record(db, "score.update" if existing else "score.submit", actor=judge, event_id=event.id,
                     target_type="project", target_id=project_id, detail={"title": project["title"]})
        webhooks.emit(db, event.id, "score.submitted",
                      {"project_id": project_id, "judge_id": judge.id, "revised": existing is not None})
    return score_id


def scores_for_judge(db: sqlite3.Connection, judge_id: str, event_id: str | None = None) -> list[dict[str, Any]]:
    """One judge's filed scorecards. The judge id is part of the query."""
    params: list[Any] = [judge_id]
    scope = ""
    if event_id:
        scope = " AND s.event_id = ?"
        params.append(event_id)
    rows = fetch_all(
        db,
        "SELECT s.id, s.event_id, s.project_id, s.comment, s.submitted_at, s.updated_at, p.title, p.track_id"
        f" FROM scores s JOIN projects p ON p.id = s.project_id WHERE s.judge_id = ?{scope} ORDER BY s.submitted_at, p.title",
        params,
    )
    if not rows:
        return []
    items = fetch_all(
        db,
        f"SELECT i.score_id, i.criterion_id, c.key, i.value FROM score_items i JOIN criteria c ON c.id = i.criterion_id"
        f" WHERE i.score_id IN ({placeholders(rows)}) ORDER BY c.position",
        [r["id"] for r in rows],
    )
    by_key: dict[str, dict[str, int]] = {}
    by_id: dict[str, dict[str, int]] = {}
    for item in items:
        by_key.setdefault(item["score_id"], {})[item["key"]] = item["value"]
        by_id.setdefault(item["score_id"], {})[item["criterion_id"]] = item["value"]
    tables: dict[str, dict] = {}
    out = []
    for row in rows:
        table = tables.setdefault(row["event_id"], rubric.weight_table(db, row["event_id"]))
        weighted = rubric.weighted_total(by_id.get(row["id"], {}), rubric.weights_for(table, row["track_id"]))
        out.append({
            "project_id": row["project_id"],
            "project_title": row["title"],
            "event_id": row["event_id"],
            "criteria": by_key.get(row["id"], {}),
            "weighted": round(weighted or 0, 4),
            "comment": row["comment"],
            "submitted_at": row["submitted_at"],
            "updated_at": row["updated_at"],
        })
    return out


def resolve_judge_id(db: sqlite3.Connection, ref: str) -> str | None:
    """Accept a user id or an email in ?judge=, return the user id if it
    names a judge anywhere."""
    row = fetch_one(db, "SELECT u.id FROM users u JOIN event_members m ON m.user_id = u.id AND m.role = 'judge'"
                        " WHERE u.id = ? OR u.email = ? LIMIT 1", (ref, ref.lower()))
    return row["id"] if row else None


def project_for_judge(db: sqlite3.Connection, judge: User, project_id: str) -> sqlite3.Row:
    try:
        project = get_project(db, project_id)
    except NotFound:
        raise Forbidden("This project is not in your judging queue.", code="not_assigned") from None
    require_scoring_access(db, judge, project)
    return project
