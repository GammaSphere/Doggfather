"""Public gallery search.

Only submitted projects are ever returned. Search is token-wise LIKE matching
over title, tagline, description, team name and tags. That is plenty for
hackathon-sized data and needs no extra index; SQLite FTS5 is the upgrade
path if a deployment ever holds tens of thousands of projects.

Filter semantics: tracks and events are OR (a project sits in exactly one of
each), tags are AND (each extra tag narrows the result).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..db import fetch_all, fetch_value, placeholders

SORTS = {
    "recent": "p.submitted_at DESC, p.id",
    "oldest": "p.submitted_at ASC, p.id",
    "title": "p.title COLLATE NOCASE ASC, p.id",
    "updated": "p.updated_at DESC, p.id",
}
SORT_LABELS = {"recent": "Newest first", "oldest": "Oldest first", "title": "A to Z", "updated": "Recently updated"}
MAX_TOKENS = 8


@dataclass
class GalleryQuery:
    q: str = ""
    event_ids: list[str] = field(default_factory=list)
    track_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    sort: str = "recent"
    page: int = 1
    per_page: int = 60

    def normalized(self) -> "GalleryQuery":
        return GalleryQuery(
            q=" ".join(self.q.split())[:120],
            event_ids=[e for e in dict.fromkeys(self.event_ids) if e][:20],
            track_ids=[t for t in dict.fromkeys(self.track_ids) if t][:40],
            tags=[t.lower() for t in dict.fromkeys(self.tags) if t][:12],
            sort=self.sort if self.sort in SORTS else "recent",
            page=max(1, self.page),
            per_page=min(96, max(1, self.per_page)),
        )


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(query: GalleryQuery) -> tuple[str, list[Any]]:
    clauses = ["p.status = 'submitted'"]
    params: list[Any] = []
    for token in query.q.split()[:MAX_TOKENS]:
        pattern = _like(token)
        clauses.append(
            "(p.title LIKE ? ESCAPE '\\' OR p.tagline LIKE ? ESCAPE '\\' OR p.description LIKE ? ESCAPE '\\'"
            " OR tm.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM project_tags pt WHERE pt.project_id = p.id"
            " AND pt.tag LIKE ? ESCAPE '\\'))"
        )
        params += [pattern] * 5
    if query.event_ids:
        clauses.append(f"p.event_id IN ({placeholders(query.event_ids)})")
        params += query.event_ids
    if query.track_ids:
        clauses.append(f"p.track_id IN ({placeholders(query.track_ids)})")
        params += query.track_ids
    for tag in query.tags:
        clauses.append("EXISTS (SELECT 1 FROM project_tags pt WHERE pt.project_id = p.id AND pt.tag = ?)")
        params.append(tag)
    return " AND ".join(clauses), params


def search(db: sqlite3.Connection, query: GalleryQuery) -> tuple[list[dict[str, Any]], int]:
    query = query.normalized()
    where, params = _where(query)
    base = f"FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id" \
           f" JOIN events e ON e.id = p.event_id WHERE {where}"
    total = fetch_value(db, f"SELECT COUNT(*) {base}", params, default=0)
    rows = fetch_all(
        db,
        f"SELECT p.id, p.title, p.tagline, p.thumbnail, p.submitted_at, p.track_id, p.event_id,"
        f" t.name AS track_name, tm.name AS team_name, e.name AS event_name {base}"
        f" ORDER BY {SORTS[query.sort]} LIMIT ? OFFSET ?",
        (*params, query.per_page, (query.page - 1) * query.per_page),
    )
    items = [dict(r) for r in rows]
    if items:
        ids = [i["id"] for i in items]
        tag_rows = fetch_all(db, f"SELECT project_id, tag FROM project_tags WHERE project_id IN ({placeholders(ids)})"
                                 " ORDER BY tag", ids)
        tags: dict[str, list[str]] = {}
        for row in tag_rows:
            tags.setdefault(row["project_id"], []).append(row["tag"])
        for item in items:
            item["tags"] = tags.get(item["id"], [])
    return items, total


def facets(db: sqlite3.Connection, event_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    scope, params = ("AND p.event_id IN (" + placeholders(event_ids) + ")", list(event_ids)) if event_ids else ("", [])
    tracks = fetch_all(
        db,
        "SELECT t.id, t.name, e.name AS event_name, COUNT(p.id) AS n FROM tracks t JOIN events e ON e.id = t.event_id"
        f" LEFT JOIN projects p ON p.track_id = t.id AND p.status = 'submitted' WHERE 1=1"
        f" {scope.replace('p.event_id', 't.event_id')} GROUP BY t.id ORDER BY e.submissions_close_at DESC, t.position",
        params,
    )
    tags = fetch_all(
        db,
        "SELECT pt.tag, COUNT(*) AS n FROM project_tags pt JOIN projects p ON p.id = pt.project_id"
        f" WHERE p.status = 'submitted' {scope} GROUP BY pt.tag ORDER BY n DESC, pt.tag LIMIT 40",
        params,
    )
    events = fetch_all(
        db,
        "SELECT e.id, e.name, COUNT(p.id) AS n FROM events e LEFT JOIN projects p ON p.event_id = e.id"
        " AND p.status = 'submitted' GROUP BY e.id ORDER BY e.submissions_close_at DESC",
    )
    return {"tracks": [dict(r) for r in tracks], "tags": [dict(r) for r in tags], "events": [dict(r) for r in events]}
