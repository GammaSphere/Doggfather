"""Organizer progress dashboard: who has finished, who is stuck.

A judge is *delinquent* when they still have pending reviews and have not
filed anything in the last ``STALE_HOURS`` (or ever). A batch is
*abandoned* when every review in it is still pending and its judge has gone
quiet. Both are the real-world failure modes the fixtures model: unfinished
review batches.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import timedelta
from typing import Any

from .. import clock
from ..db import fetch_all
from .events import Event

STALE_HOURS = 24


def snapshot(db: sqlite3.Connection, event: Event) -> dict[str, Any]:
    now = clock.now()
    stale_before = clock.iso(now - timedelta(hours=STALE_HOURS))
    rows = fetch_all(
        db,
        "SELECT a.judge_id, a.project_id, a.status, a.batch, a.assigned_at, u.name, u.email, p.track_id,"
        " s.updated_at AS scored_at FROM assignments a JOIN users u ON u.id = a.judge_id"
        " JOIN projects p ON p.id = a.project_id LEFT JOIN scores s ON s.assignment_id = a.id"
        " WHERE a.event_id = ? AND p.status = 'submitted'",
        (event.id,),
    )
    judges: dict[str, dict[str, Any]] = {}
    batches: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"pending": 0, "done": 0})
    for r in rows:
        j = judges.setdefault(r["judge_id"], {"id": r["judge_id"], "name": r["name"], "email": r["email"],
                                               "assigned": 0, "done": 0, "last_activity": None})
        j["assigned"] += 1
        batches[(r["judge_id"], r["batch"])][r["status"]] += 1
        if r["status"] == "done":
            j["done"] += 1
            if r["scored_at"] and (j["last_activity"] is None or r["scored_at"] > j["last_activity"]):
                j["last_activity"] = r["scored_at"]

    judge_list = []
    for j in judges.values():
        pending = j["assigned"] - j["done"]
        quiet = j["last_activity"] is None or j["last_activity"] < stale_before
        if pending == 0:
            state = "finished"
        elif j["done"] == 0:
            state = "not started"
        elif quiet:
            state = "stalled"
        else:
            state = "active"
        abandoned = [batch for (jid, batch), c in batches.items() if jid == j["id"] and c["done"] == 0 and c["pending"] > 0]
        judge_list.append({**j, "pending": pending, "state": state, "delinquent": pending > 0 and quiet,
                           "abandoned_batches": sorted(abandoned),
                           "pct": round(100 * j["done"] / j["assigned"]) if j["assigned"] else 100})
    judge_list.sort(key=lambda j: (not j["delinquent"], -j["pending"], j["name"]))

    tracks = {t["id"]: {"id": t["id"], "name": t["name"], "projects": 0, "assigned": 0, "done": 0, "short": 0}
              for t in fetch_all(db, "SELECT id, name FROM tracks WHERE event_id = ? ORDER BY position", (event.id,))}
    per_project: dict[str, dict[str, int]] = defaultdict(lambda: {"assigned": 0, "done": 0})
    for r in rows:
        per_project[r["project_id"]]["assigned"] += 1
        per_project[r["project_id"]]["done"] += int(r["status"] == "done")
    projects = fetch_all(db, "SELECT p.id, p.title, p.track_id FROM projects p WHERE p.event_id = ? AND p.status = 'submitted'",
                         (event.id,))
    below = []
    for p in projects:
        counts = per_project[p["id"]]
        track = tracks.get(p["track_id"])
        if track:
            track["projects"] += 1
            track["assigned"] += counts["assigned"]
            track["done"] += counts["done"]
        if counts["done"] < event.review_target:
            below.append({"id": p["id"], "title": p["title"], "done": counts["done"], "assigned": counts["assigned"]})
            if track:
                track["short"] += 1
    for track in tracks.values():
        track["pct"] = round(100 * track["done"] / track["assigned"]) if track["assigned"] else 0

    assigned = sum(j["assigned"] for j in judge_list)
    done = sum(j["done"] for j in judge_list)
    hours_left = (clock.parse(event.judging_close_at) - now).total_seconds() / 3600
    return {
        "generated_at": clock.iso(now),
        "judging_open": event.judging_open(),
        "judging_close_at": event.judging_close_at,
        "hours_left": round(hours_left, 1),
        "totals": {
            "assigned": assigned, "done": done, "pending": assigned - done,
            "pct": round(100 * done / assigned) if assigned else 0,
            "delinquent": sum(1 for j in judge_list if j["delinquent"]),
            "finished_judges": sum(1 for j in judge_list if j["state"] == "finished"),
            "judges": len(judge_list),
            "projects_below_target": len(below),
        },
        "judges": judge_list,
        "tracks": list(tracks.values()),
        "below_target": sorted(below, key=lambda b: (b["done"], b["title"]))[:50],
    }
