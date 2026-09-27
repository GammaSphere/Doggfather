"""Duplicate submission detection.

A later submission is flagged as a possible duplicate of an earlier one when
any of these hold:

* the same repository (compared after stripping scheme, ``www.``, ``.git``
  and trailing slashes),
* the same title (compared on letters and digits only),
* near-identical long descriptions (word 3-gram Jaccard >= 0.8, both at
  least 80 characters). The length floor exists because the fixtures give
  every project the same one-line summary, which must not flag all 41.

Flags are advisory. An organizer dismisses a flag (which sticks) or
withdraws the project. Fixture prj_41 is caught on all three signals
against prj_07: same team, title and repo.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from itertools import combinations

from .. import audit, clock, policy
from ..auth import User
from ..db import fetch_all, fetch_one, transaction
from ..errors import NotFound
from .events import Event

MIN_DESCRIPTION = 80
SIMILARITY = 0.8


@dataclass(frozen=True)
class Flag:
    project_id: str
    original_id: str
    reasons: tuple[str, ...]


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def normalize_repo(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^[a-z]+://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = re.sub(r"(\.git)?/*$", "", url)
    return url


def shingles(text: str, k: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def detect(db: sqlite3.Connection, event_id: str) -> list[Flag]:
    projects = fetch_all(db, "SELECT id, title, repo_url, description, submitted_at FROM projects"
                             " WHERE event_id = ? AND status = 'submitted' ORDER BY submitted_at, id", (event_id,))
    reasons: dict[tuple[str, str], list[str]] = {}

    def mark(earlier: str, later: str, reason: str) -> None:
        reasons.setdefault((earlier, later), []).append(reason)

    by_repo: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for p in projects:
        repo = normalize_repo(p["repo_url"] or "")
        if repo:
            if repo in by_repo:
                mark(by_repo[repo], p["id"], "same repository")
            else:
                by_repo[repo] = p["id"]
        title = normalize_title(p["title"])
        if len(title) >= 3:
            if title in by_title:
                mark(by_title[title], p["id"], "same title")
            else:
                by_title[title] = p["id"]
    long = [(p["id"], shingles(p["description"])) for p in projects if len(p["description"] or "") >= MIN_DESCRIPTION]
    for (a, sa), (b, sb) in combinations(long, 2):
        if jaccard(sa, sb) >= SIMILARITY:
            mark(a, b, "near-identical description")
    flags: dict[str, Flag] = {}
    for (earlier, later), why in reasons.items():
        if later not in flags:
            flags[later] = Flag(later, earlier, tuple(why))
    return list(flags.values())


def refresh(db: sqlite3.Connection, event_id: str, actor: User | None = None) -> list[Flag]:
    """Write current flags onto projects; dismissed ones stay dismissed."""
    found = detect(db, event_id)
    with transaction(db):
        existing = {r["id"]: r for r in fetch_all(
            db, "SELECT p.id, p.title, p.duplicate_of, p.duplicate_dismissed_at, o.title AS other"
                " FROM projects p LEFT JOIN projects o ON o.id = p.duplicate_of WHERE p.event_id = ?", (event_id,))}
        flagged_ids = set()
        for flag in found:
            row = existing.get(flag.project_id)
            if row is None or row["duplicate_dismissed_at"]:
                continue
            flagged_ids.add(flag.project_id)
            if row["duplicate_of"] != flag.original_id:
                db.execute("UPDATE projects SET duplicate_of = ?, duplicate_reason = ? WHERE id = ?",
                           (flag.original_id, ", ".join(flag.reasons), flag.project_id))
                other = existing[flag.original_id]["title"] if flag.original_id in existing else flag.original_id
                audit.record(db, "project.duplicate_flag", actor=actor, event_id=event_id, target_type="project",
                             target_id=flag.project_id,
                             detail={"title": row["title"], "other": other, "reason": ", ".join(flag.reasons)})
        for pid, row in existing.items():
            if row["duplicate_of"] and pid not in flagged_ids and not row["duplicate_dismissed_at"]:
                db.execute("UPDATE projects SET duplicate_of = NULL, duplicate_reason = NULL WHERE id = ?", (pid,))
    return found


def flagged(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT p.id, p.title, p.duplicate_reason, p.submitted_at, p.status, tm.name AS team_name,"
        " o.id AS original_id, o.title AS original_title, o.submitted_at AS original_submitted_at"
        " FROM projects p JOIN projects o ON o.id = p.duplicate_of JOIN teams tm ON tm.id = p.team_id"
        " WHERE p.event_id = ? ORDER BY p.submitted_at",
        (event_id,),
    )


def dismiss(db: sqlite3.Connection, actor: User, event: Event, project_id: str) -> None:
    policy.require_organizer(db, actor, event.id)
    row = fetch_one(db, "SELECT title FROM projects WHERE id = ? AND event_id = ?", (project_id, event.id))
    if row is None:
        raise NotFound("No such project.")
    with transaction(db):
        db.execute("UPDATE projects SET duplicate_of = NULL, duplicate_reason = NULL, duplicate_dismissed_at = ?"
                   " WHERE id = ?", (clock.now_iso(), project_id))
        audit.record(db, "project.duplicate_clear", actor=actor, event_id=event.id, target_type="project",
                     target_id=project_id, detail={"title": row["title"]})


def withdraw(db: sqlite3.Connection, actor: User, event: Event, project_id: str, reason: str) -> None:
    """Organizer removes an entry (duplicate, ineligible). It leaves the
    gallery and the results but stays in the database and the audit log."""
    policy.require_organizer(db, actor, event.id)
    row = fetch_one(db, "SELECT title FROM projects WHERE id = ? AND event_id = ?", (project_id, event.id))
    if row is None:
        raise NotFound("No such project.")
    reason = (reason or "withdrawn by the organizers").strip()[:200]
    with transaction(db):
        db.execute("UPDATE projects SET status = 'withdrawn', withdrawn_reason = ?, updated_at = ? WHERE id = ?",
                   (reason, clock.now_iso(), project_id))
        audit.record(db, "project.withdraw", actor=actor, event_id=event.id, target_type="project",
                     target_id=project_id, detail={"title": row["title"], "reason": reason})
