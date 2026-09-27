"""Pairwise judging: choose the next pair, record the verdict, rank.

Absolute scores ask a judge to hold a calibrated 1-5 scale in their head for
hours. Pairwise judging asks the easier question, "which of these two is
better?", and recovers a global ranking with Bradley-Terry (see pairwise.py).
Gavel (HackMIT) popularised this for hackathons.

Pair selection is active, not random:

1. the first project is the one in the judge's pool with the fewest
   comparisons so far (coverage),
2. its opponent is the not-yet-compared project whose current BT strength is
   closest (the most informative match: a near coin-flip teaches the model
   the most),
3. left/right order is a stable hash, so position cannot bias the verdict.

A judge's pool is their tracks plus anything assigned to them. The same
isolation rules as scoring apply.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from typing import Any

from .. import audit, clock, policy
from ..auth import User
from ..db import fetch_all, new_id, placeholders, transaction
from ..errors import Conflict, Forbidden
from .events import Event
from .pairwise import bradley_terry


def _hash(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def pool(db: sqlite3.Connection, judge_id: str, event: Event) -> list[str]:
    tracks = policy.judge_track_ids(db, judge_id, event.id)
    rows = fetch_all(
        db,
        "SELECT p.id FROM projects p WHERE p.event_id = ? AND p.status = 'submitted' AND ("
        + (f"p.track_id IN ({placeholders(tracks)}) OR " if tracks else "")
        + "p.id IN (SELECT project_id FROM assignments WHERE judge_id = ?)) ORDER BY p.id",
        (event.id, *tracks, judge_id),
    )
    return [r["id"] for r in rows]


def event_comparisons(db: sqlite3.Connection, event_id: str) -> list[tuple[str, str, float]]:
    rows = fetch_all(db, "SELECT winner_id, loser_id FROM pairwise_comparisons WHERE event_id = ?", (event_id,))
    return [(r["winner_id"], r["loser_id"], 1.0) for r in rows]


def _judged_pairs(db: sqlite3.Connection, judge_id: str, event_id: str) -> set[frozenset[str]]:
    rows = fetch_all(db, "SELECT winner_id, loser_id FROM pairwise_comparisons WHERE judge_id = ? AND event_id = ?",
                     (judge_id, event_id))
    return {frozenset((r["winner_id"], r["loser_id"])) for r in rows}


def _require_mode(db: sqlite3.Connection, judge: User, event: Event) -> None:
    policy.require_judge(db, judge, event.id)
    if not event.pairwise_enabled:
        raise Forbidden("Pairwise judging is not enabled for this event.", code="pairwise_disabled")


def next_pair(db: sqlite3.Connection, judge: User, event: Event) -> tuple[str, str] | None:
    _require_mode(db, judge, event)
    candidates = pool(db, judge.id, event)
    if len(candidates) < 2:
        return None
    done = _judged_pairs(db, judge.id, event.id)
    comparisons = event_comparisons(db, event.id)
    counts: dict[str, int] = defaultdict(int)
    for winner, loser, _ in comparisons:
        counts[winner] += 1
        counts[loser] += 1
    theta = bradley_terry(comparisons, candidates)

    def open_opponents(a: str) -> list[str]:
        return [b for b in candidates if b != a and frozenset((a, b)) not in done]

    firsts = sorted((a for a in candidates if open_opponents(a)),
                    key=lambda a: (counts[a], _hash(judge.id, "first", a)))
    if not firsts:
        return None
    a = firsts[0]
    b = min(open_opponents(a), key=lambda x: (abs(theta.get(a, 0) - theta.get(x, 0)), counts[x], _hash(judge.id, a, x)))
    return (a, b) if _hash(judge.id, "side", a, b) < _hash(judge.id, "side", b, a) else (b, a)


def record(db: sqlite3.Connection, judge: User, event: Event, winner_id: str, loser_id: str) -> str:
    _require_mode(db, judge, event)
    if not event.judging_open():
        raise Forbidden("Judging is not open.", code="judging_closed")
    candidates = set(pool(db, judge.id, event))
    if winner_id == loser_id or winner_id not in candidates or loser_id not in candidates:
        raise Forbidden("Those projects are not in your judging pool.", code="not_assigned")
    if frozenset((winner_id, loser_id)) in _judged_pairs(db, judge.id, event.id):
        raise Conflict("You already compared these two.", code="already_compared")
    comparison_id = new_id("cmp")
    with transaction(db):
        db.execute("INSERT INTO pairwise_comparisons (id, event_id, judge_id, winner_id, loser_id, created_at)"
                   " VALUES (?, ?, ?, ?, ?, ?)", (comparison_id, event.id, judge.id, winner_id, loser_id, clock.now_iso()))
        audit.record(db, "pairwise.compare", actor=judge, event_id=event.id, target_type="comparison",
                     target_id=comparison_id)
    return comparison_id


def judge_progress(db: sqlite3.Connection, judge_id: str, event: Event) -> dict[str, int]:
    n = len(pool(db, judge_id, event))
    done = len(_judged_pairs(db, judge_id, event.id))
    return {"done": done, "possible": n * (n - 1) // 2}


def ranking(db: sqlite3.Connection, actor: User, event: Event) -> list[dict[str, Any]]:
    policy.require_organizer(db, actor, event.id)
    comparisons = event_comparisons(db, event.id)
    projects = fetch_all(db, "SELECT id, title FROM projects WHERE event_id = ? AND status = 'submitted'", (event.id,))
    theta = bradley_terry(comparisons, [p["id"] for p in projects])
    wins: dict[str, int] = defaultdict(int)
    games: dict[str, int] = defaultdict(int)
    for winner, loser, _ in comparisons:
        wins[winner] += 1
        games[winner] += 1
        games[loser] += 1
    rows = [{"id": p["id"], "title": p["title"], "theta": theta.get(p["id"], 0.0), "wins": wins[p["id"]],
             "comparisons": games[p["id"]]} for p in projects]
    rows.sort(key=lambda r: (-r["theta"], -r["wins"], r["title"]))
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows
