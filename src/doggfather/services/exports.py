"""CSV exports for every stage of the pipeline.

All exports are organizer-only (they carry emails and raw scores) and
audited. Cells that a spreadsheet would treat as a formula are prefixed
with an apostrophe (CSV injection, OWASP), so a project called
``=HYPERLINK(...)`` stays text.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Iterable

from .. import audit
from ..auth import User
from ..db import fetch_all
from ..errors import NotFound
from ..policy import require_organizer
from . import judges as judge_service
from . import results, rubric
from .events import Event

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
KINDS = {
    "results": "Ranked results with every normalization method",
    "scores": "Every scorecard, one row per judge and project",
    "projects": "Submissions with links, tags and status",
    "teams": "Teams and their members",
    "judges": "Judges, tracks and workload",
    "assignments": "Who reviews what, by batch",
    "audit": "The audit trail, in plain English",
}


def safe_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    text = str(value)
    if text.startswith(FORMULA_PREFIXES) and not _is_number(text):
        return "'" + text
    return text


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def to_csv(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return buffer.getvalue()


def _results(db: sqlite3.Connection, event: Event, method: str | None) -> str:
    table = results.compute_table(db, event, method)
    headers = ["rank", "project_id", "title", "team", "track", "reviews", "score", "method", "movement_vs_raw",
               "raw_mean", "raw_rank", "zscore", "zscore_rank", "bias_model", "bias_rank", "bt_theta", "bt_rank"]
    rows = ([r["rank"], r["id"], r["title"], r["team_name"], r["track_name"], r["reviews"], r["score"], table.method,
             r["movement"], r["raw"], r["raw_rank"], r["zscore"], r["zscore_rank"], r["bias"], r["bias_rank"],
             r["pairwise"], r["pairwise_rank"]] for r in table.rows)
    return to_csv(headers, rows)


def _scores(db: sqlite3.Connection, event: Event) -> str:
    criteria = rubric.list_criteria(db, event.id)
    table = rubric.weight_table(db, event.id)
    cards = fetch_all(
        db,
        "SELECT s.id, s.judge_id, u.name AS judge_name, s.project_id, p.title, p.track_id, t.name AS track_name,"
        " s.comment, s.submitted_at FROM scores s JOIN users u ON u.id = s.judge_id JOIN projects p ON p.id = s.project_id"
        " LEFT JOIN tracks t ON t.id = p.track_id WHERE s.event_id = ? ORDER BY p.title, u.name",
        (event.id,),
    )
    items: dict[str, dict[str, int]] = {}
    for row in fetch_all(db, "SELECT i.score_id, i.criterion_id, i.value FROM score_items i JOIN scores s ON s.id = i.score_id"
                             " WHERE s.event_id = ?", (event.id,)):
        items.setdefault(row["score_id"], {})[row["criterion_id"]] = row["value"]
    headers = ["judge_id", "judge_name", "project_id", "project_title", "track"] + [c["key"] for c in criteria] + \
              ["weighted", "comment", "submitted_at"]
    rows = []
    for card in cards:
        values = items.get(card["id"], {})
        weighted = rubric.weighted_total(values, rubric.weights_for(table, card["track_id"]))
        rows.append([card["judge_id"], card["judge_name"], card["project_id"], card["title"], card["track_name"]]
                    + [values.get(c["id"]) for c in criteria] + [weighted, card["comment"], card["submitted_at"]])
    return to_csv(headers, rows)


def _projects(db: sqlite3.Connection, event: Event) -> str:
    rows = fetch_all(
        db,
        "SELECT p.*, tm.name AS team_name, t.name AS track_name,"
        " (SELECT GROUP_CONCAT(tag, ' ') FROM project_tags WHERE project_id = p.id) AS tags"
        " FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id"
        " WHERE p.event_id = ? ORDER BY p.submitted_at, p.id",
        (event.id,),
    )
    headers = ["project_id", "title", "tagline", "team_id", "team", "track", "status", "submitted_at", "updated_at",
               "repo_url", "demo_url", "video_url", "tags", "duplicate_of", "duplicate_reason"]
    return to_csv(headers, ([r["id"], r["title"], r["tagline"], r["team_id"], r["team_name"], r["track_name"], r["status"],
                             r["submitted_at"], r["updated_at"], r["repo_url"], r["demo_url"], r["video_url"], r["tags"],
                             r["duplicate_of"], r["duplicate_reason"]] for r in rows))


def _teams(db: sqlite3.Connection, event: Event) -> str:
    rows = fetch_all(
        db,
        "SELECT t.id, t.name, t.created_at, COUNT(m.user_id) AS size,"
        " GROUP_CONCAT(u.email, ';') AS emails,"
        " MAX(CASE WHEN m.role = 'captain' THEN u.email END) AS captain"
        " FROM teams t LEFT JOIN team_members m ON m.team_id = t.id LEFT JOIN users u ON u.id = m.user_id"
        " WHERE t.event_id = ? GROUP BY t.id ORDER BY t.id",
        (event.id,),
    )
    return to_csv(["team_id", "name", "size", "captain", "member_emails", "created_at"],
                  ([r["id"], r["name"], r["size"], r["captain"], r["emails"], r["created_at"]] for r in rows))


def _judges(db: sqlite3.Connection, event: Event) -> str:
    return to_csv(["judge_id", "name", "email", "tracks", "assigned", "done", "last_scored_at"],
                  ([j["id"], j["name"], j["email"], ";".join(t["name"] for t in j["tracks"]), j["assigned"], j["done"],
                    j["last_scored_at"]] for j in judge_service.roster(db, event.id)))


def _assignments(db: sqlite3.Connection, event: Event) -> str:
    rows = fetch_all(
        db,
        "SELECT a.id, a.judge_id, u.name, a.project_id, p.title, a.batch, a.status, a.assigned_at FROM assignments a"
        " JOIN users u ON u.id = a.judge_id JOIN projects p ON p.id = a.project_id WHERE a.event_id = ?"
        " ORDER BY a.batch, u.name, p.title",
        (event.id,),
    )
    return to_csv(["assignment_id", "judge_id", "judge_name", "project_id", "project_title", "batch", "status", "assigned_at"],
                  ([r["id"], r["judge_id"], r["name"], r["project_id"], r["title"], r["batch"], r["status"],
                    r["assigned_at"]] for r in rows))


def _audit(db: sqlite3.Connection, event: Event) -> str:
    entries = audit.entries(db, event_id=event.id, limit=100000)
    return to_csv(["id", "at", "actor", "action", "sentence", "target_type", "target_id", "hash"],
                  ([e["id"], e["at"], e["actor_label"], e["action"], e["sentence"], e["target_type"], e["target_id"],
                    e["hash"]] for e in reversed(entries)))


BUILDERS = {
    "projects": _projects, "teams": _teams, "judges": _judges, "assignments": _assignments,
    "scores": _scores, "audit": _audit,
}


def export(db: sqlite3.Connection, actor: User | None, event: Event, kind: str, method: str | None = None) -> str:
    require_organizer(db, actor, event.id)
    if kind == "results":
        text = _results(db, event, method)
    elif kind in BUILDERS:
        text = BUILDERS[kind](db, event)
    else:
        raise NotFound(f"Unknown export '{kind}'. Try one of: {', '.join(KINDS)}.")
    audit.record(db, "export.csv", actor=actor, event_id=event.id, target_type="export", target_id=kind,
                 detail={"kind": kind})
    return text
