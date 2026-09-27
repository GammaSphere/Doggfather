"""Reviewer assignment.

Two modes:

* **Batch**: an organizer hands a judge a hand-picked set of projects.
* **Automatic**: a track-aware greedy that tops every submitted project up to
  the event's review target (k reviews).

The automatic pass visits projects fewest-reviews-first. For each one it
picks, from the judges who cover the project's track and are not already on
it, the candidate with:

1. the lightest current load (spreads the work, bounds the slowest judge), then
2. the fewest projects already shared with the judges on this one, which
   spreads overlap across many judge pairs instead of forming cliques. The
   normalization model needs a *connected* judge graph to tell a harsh judge
   from a weak batch; overlap diversity is what connects it,
3. a stable hash of (seed, judge, project) to break remaining ties, so the
   same inputs always give the same plan.

Conflicts of interest (a judge on the project's team) are impossible by
schema, and checked again here. Projects that cannot reach k (too few
eligible judges) are reported as a shortfall rather than silently filled
from other tracks.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .. import audit, clock
from ..auth import User
from ..db import fetch_all, fetch_one, new_id, placeholders, transaction
from ..errors import Conflict, NotFound, ValidationFailed
from ..policy import require_organizer
from .events import Event


@dataclass
class AssignmentPlan:
    batch: str
    created: list[tuple[str, str]] = field(default_factory=list)
    shortfall: dict[str, int] = field(default_factory=dict)


def next_batch_label(db: sqlite3.Connection, event_id: str) -> str:
    rows = fetch_all(db, "SELECT DISTINCT batch FROM assignments WHERE event_id = ? AND batch LIKE 'B-%'", (event_id,))
    numbers = [int(r["batch"][2:]) for r in rows if r["batch"][2:].isdigit()]
    return f"B-{(max(numbers) if numbers else 0) + 1:03d}"


def _tiebreak(seed: str, judge_id: str, project_id: str) -> str:
    return hashlib.sha256(f"{seed}:{judge_id}:{project_id}".encode()).hexdigest()


def plan_auto(db: sqlite3.Connection, event: Event, target: int | None = None, seed: str = "doggfather") -> AssignmentPlan:
    target = target or event.review_target
    projects = fetch_all(db, "SELECT p.id, p.track_id, p.team_id FROM projects p WHERE p.event_id = ?"
                             " AND p.status = 'submitted' ORDER BY p.id", (event.id,))
    eligible: dict[str, list[str]] = defaultdict(list)
    for row in fetch_all(db, "SELECT jt.track_id, jt.user_id FROM judge_tracks jt JOIN event_members m"
                             " ON m.user_id = jt.user_id AND m.event_id = jt.event_id AND m.role = 'judge'"
                             " WHERE jt.event_id = ? ORDER BY jt.user_id", (event.id,)):
        eligible[row["track_id"]].append(row["user_id"])
    team_members: dict[str, set[str]] = defaultdict(set)
    for row in fetch_all(db, "SELECT team_id, user_id FROM team_members WHERE event_id = ?", (event.id,)):
        team_members[row["team_id"]].add(row["user_id"])

    on_project: dict[str, set[str]] = defaultdict(set)
    load: dict[str, int] = defaultdict(int)
    for row in fetch_all(db, "SELECT judge_id, project_id FROM assignments WHERE event_id = ?", (event.id,)):
        on_project[row["project_id"]].add(row["judge_id"])
        load[row["judge_id"]] += 1
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for judges in on_project.values():
        for a in judges:
            for b in judges:
                if a != b:
                    shared[(a, b)] += 1

    plan = AssignmentPlan(batch=next_batch_label(db, event.id))
    order = sorted(projects, key=lambda p: (len(on_project[p["id"]]), p["id"]))
    for project in order:
        pid = project["id"]
        need = target - len(on_project[pid])
        if need <= 0:
            continue
        candidates = [j for j in eligible.get(project["track_id"], [])
                      if j not in on_project[pid] and j not in team_members[project["team_id"]]]
        while need > 0 and candidates:
            best = min(candidates, key=lambda j: (load[j], sum(shared[(j, k)] for k in on_project[pid]),
                                                  _tiebreak(seed, j, pid)))
            candidates.remove(best)
            for other in on_project[pid]:
                shared[(best, other)] += 1
                shared[(other, best)] += 1
            on_project[pid].add(best)
            load[best] += 1
            plan.created.append((best, pid))
            need -= 1
        if need > 0:
            plan.shortfall[pid] = need
    return plan


def run_auto(db: sqlite3.Connection, actor: User | None, event: Event, target: int | None = None) -> AssignmentPlan:
    if actor is not None:
        require_organizer(db, actor, event.id)
    if target is not None and not 1 <= target <= 20:
        raise ValidationFailed(fields={"target": "Between 1 and 20 reviews per project."})
    now = clock.now_iso()
    with transaction(db):
        plan = plan_auto(db, event, target)
        db.executemany(
            "INSERT INTO assignments (id, event_id, judge_id, project_id, batch, assigned_by, assigned_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(new_id("asg"), event.id, j, p, plan.batch, actor.id if actor else None, now) for j, p in plan.created],
        )
        audit.record(db, "assign.auto", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"batch": plan.batch, "created": len(plan.created), "shortfall": len(plan.shortfall)})
    return plan


def assign_batch(db: sqlite3.Connection, actor: User, event: Event, judge_id: str, project_ids: list[str]) -> str:
    require_organizer(db, actor, event.id)
    judge = fetch_one(db, "SELECT u.name FROM event_members m JOIN users u ON u.id = m.user_id"
                          " WHERE m.event_id = ? AND m.user_id = ? AND m.role = 'judge'", (event.id, judge_id))
    if judge is None:
        raise ValidationFailed(fields={"judge": "Pick a judge from this event."})
    project_ids = [p for p in dict.fromkeys(project_ids) if p]
    if not project_ids:
        raise ValidationFailed(fields={"projects": "Pick at least one project."})
    rows = fetch_all(db, f"SELECT id, team_id FROM projects WHERE event_id = ? AND status = 'submitted'"
                         f" AND id IN ({placeholders(project_ids)})", (event.id, *project_ids))
    if len(rows) != len(project_ids):
        raise ValidationFailed(fields={"projects": "Only submitted projects from this event can be assigned."})
    for row in rows:
        if fetch_one(db, "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (row["team_id"], judge_id)):
            raise Conflict("That judge is on one of those teams.", code="conflict_of_interest")
    batch = next_batch_label(db, event.id)
    now = clock.now_iso()
    with transaction(db):
        created = 0
        for pid in project_ids:
            cursor = db.execute(
                "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, project_id, batch, assigned_by, assigned_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id("asg"), event.id, judge_id, pid, batch, actor.id, now))
            created += cursor.rowcount
        audit.record(db, "assign.batch", actor=actor, event_id=event.id, target_type="user", target_id=judge_id,
                     detail={"judge": judge["name"], "count": created, "batch": batch})
    return batch


def unassign(db: sqlite3.Connection, actor: User, event: Event, assignment_id: str) -> None:
    require_organizer(db, actor, event.id)
    row = fetch_one(db, "SELECT a.*, p.title, u.name AS judge_name FROM assignments a JOIN projects p ON p.id = a.project_id"
                        " JOIN users u ON u.id = a.judge_id WHERE a.id = ? AND a.event_id = ?", (assignment_id, event.id))
    if row is None:
        raise NotFound("No such assignment.")
    if row["status"] == "done":
        raise Conflict("That review is already filed; it stays on the record.")
    with transaction(db):
        db.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
        audit.record(db, "assign.remove", actor=actor, event_id=event.id, target_type="assignment",
                     target_id=assignment_id, detail={"title": row["title"], "judge": row["judge_name"]})


def matrix(db: sqlite3.Connection, event_id: str) -> list[dict]:
    """Per project: who reviews it and how far along. Organizer-only."""
    projects = fetch_all(db, "SELECT p.id, p.title, t.name AS track_name FROM projects p LEFT JOIN tracks t"
                             " ON t.id = p.track_id WHERE p.event_id = ? AND p.status = 'submitted'"
                             " ORDER BY t.position, p.title", (event_id,))
    reviews: dict[str, list[dict]] = defaultdict(list)
    for row in fetch_all(db, "SELECT a.id, a.project_id, a.status, a.batch, u.id AS judge_id, u.name AS judge_name"
                             " FROM assignments a JOIN users u ON u.id = a.judge_id WHERE a.event_id = ?"
                             " ORDER BY a.status DESC, u.name", (event_id,)):
        reviews[row["project_id"]].append(dict(row))
    return [{**dict(p), "reviews": reviews[p["id"]],
             "done": sum(1 for r in reviews[p["id"]] if r["status"] == "done")} for p in projects]
