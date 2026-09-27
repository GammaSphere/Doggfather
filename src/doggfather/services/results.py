"""Aggregate results: every judge's scores, normalized and ranked.

This is the only module that reads *all* scorecards of an event. Its entry
points take the acting user and check ``policy`` first. Aggregates are
organizer-only until results are published.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Any

from .. import audit, clock, policy
from ..auth import User
from ..db import fetch_all, transaction
from ..errors import Conflict, ResultsHidden, ValidationFailed
from . import normalization, rubric, webhooks
from .events import Event, get_event
from .normalization import METHODS, Observation


def observations(db: sqlite3.Connection, event_id: str) -> list[Observation]:
    """One observation per filed scorecard: its weighted total, using the
    weights of the project's track."""
    table = rubric.weight_table(db, event_id)
    rows = fetch_all(
        db,
        "SELECT s.id, s.judge_id, s.project_id, p.track_id, i.criterion_id, i.value FROM scores s"
        " JOIN projects p ON p.id = s.project_id JOIN score_items i ON i.score_id = s.id"
        " WHERE s.event_id = ? AND p.status = 'submitted' ORDER BY s.id",
        (event_id,),
    )
    cards: dict[str, dict[str, Any]] = {}
    for row in rows:
        card = cards.setdefault(row["id"], {"judge": row["judge_id"], "project": row["project_id"],
                                            "track": row["track_id"], "values": {}})
        card["values"][row["criterion_id"]] = row["value"]
    out = []
    for card in cards.values():
        total = rubric.weighted_total(card["values"], rubric.weights_for(table, card["track"]))
        if total is not None:
            out.append(Observation(card["judge"], card["project"], total))
    return out


@dataclass
class ResultsTable:
    event: Event
    method: str
    rows: list[dict[str, Any]]
    judges: list[dict[str, Any]]
    components: list[set[str]]
    info: dict[str, Any] = field(default_factory=dict)


def compute_table(db: sqlite3.Connection, event: Event, method: str | None = None) -> ResultsTable:
    chosen: str = method if method is not None and method in METHODS else event.normalization
    obs = observations(db, event.id)
    projects = fetch_all(
        db,
        "SELECT p.id, p.title, p.submitted_at, p.track_id, p.duplicate_of, t.name AS track_name, tm.name AS team_name"
        " FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id"
        " WHERE p.event_id = ? AND p.status = 'submitted'",
        (event.id,),
    )
    results = {m: normalization.compute(m, obs) for m in METHODS}
    reviews: dict[str, int] = {}
    for o in obs:
        reviews[o.project] = reviews.get(o.project, 0) + 1
    raw = results["raw"].scores
    # Ties on the chosen score break on raw mean, then review count, then who submitted first.
    tiebreak = {p["id"]: (-raw.get(p["id"], 0), -reviews.get(p["id"], 0), p["submitted_at"] or "") for p in projects}
    ranks = {m: normalization.rank({p: s for p, s in results[m].scores.items()}, tiebreak) for m in METHODS}

    rows = []
    for p in projects:
        pid = p["id"]
        row = {**dict(p), "reviews": reviews.get(pid, 0)}
        for m in METHODS:
            row[m] = results[m].scores.get(pid)
            row[f"{m}_rank"] = ranks[m].get(pid)
        row["score"] = row[chosen]
        row["rank"] = row[f"{chosen}_rank"]
        row["movement"] = (row["raw_rank"] - row["rank"]) if row["rank"] and row["raw_rank"] else 0
        rows.append(row)
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, tiebreak.get(r["id"], ()), r["title"]))

    names = {r["id"]: r["name"] for r in fetch_all(
        db, "SELECT u.id, u.name FROM event_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.event_id = ? AND m.role = 'judge'", (event.id,))}
    by_judge: dict[str, list[float]] = {}
    for o in obs:
        by_judge.setdefault(o.judge, []).append(o.score)
    judges = []
    for judge_id, scores in sorted(by_judge.items(), key=lambda kv: names.get(kv[0], kv[0])):
        sd = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        z = results["zscore"].judge_detail.get(judge_id, {})
        judges.append({
            "id": judge_id, "name": names.get(judge_id, judge_id), "n": len(scores),
            "mean": statistics.fmean(scores), "sd": sd,
            "bias": results["bias"].judge_detail.get(judge_id, {}).get("bias", 0.0),
            "shrunk_sd": z.get("shrunk_sd"),
            "flat": len(scores) >= 2 and sd == 0,
            "thin": len(scores) < 3,
        })
    info = {
        "observations": len(obs),
        "grand_mean": statistics.fmean(o.score for o in obs) if obs else None,
        "bias": results["bias"].info,
        "zscore": results["zscore"].info,
        "pairwise": results["pairwise"].info,
        "agreement": {m: normalization.spearman(results[m].scores, raw) for m in METHODS if m != "raw"},
    }
    return ResultsTable(event, chosen, rows, judges, normalization.judge_components(obs), info)


# ------------------------------------------------------------ visibility

def organizer_table(db: sqlite3.Connection, actor: User, event: Event, method: str | None = None) -> ResultsTable:
    policy.require_organizer(db, actor, event.id)
    return compute_table(db, event, method)


def visible_table(db: sqlite3.Connection, actor: User | None, event: Event) -> ResultsTable:
    """Public view: only after publication, only in the published method."""
    if policy.is_organizer(db, actor, event.id):
        return compute_table(db, event)
    if not event.results_published:
        raise ResultsHidden()
    return compute_table(db, event)


# ------------------------------------------------------------ publishing

def set_method(db: sqlite3.Connection, actor: User, event: Event, method: str) -> Event:
    policy.require_organizer(db, actor, event.id)
    if method not in METHODS:
        raise ValidationFailed(fields={"method": "Unknown method."})
    if event.results_published:
        raise Conflict("Unpublish before changing the ranking method.")
    with transaction(db):
        db.execute("UPDATE events SET normalization = ?, updated_at = ? WHERE id = ?", (method, clock.now_iso(), event.id))
        audit.record(db, "results.method", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"method": normalization.METHOD_LABELS[method]})
    return get_event(db, event.id)


def publish(db: sqlite3.Connection, actor: User, event: Event) -> Event:
    policy.require_organizer(db, actor, event.id)
    if event.results_published:
        return event
    if clock.now_iso() < event.judging_close_at:
        raise Conflict("Close judging before publishing results (Overview → phase controls).")
    with transaction(db):
        db.execute("UPDATE events SET results_published_at = ?, updated_at = ? WHERE id = ?",
                   (clock.now_iso(), clock.now_iso(), event.id))
        audit.record(db, "results.publish", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"method": normalization.METHOD_LABELS[event.normalization]})
        top = compute_table(db, event).rows[:10]
        webhooks.emit(db, event.id, "results.published", {
            "method": event.normalization, "url": f"/events/{event.slug}/results",
            "top": [{"rank": r["rank"], "project_id": r["id"], "title": r["title"]} for r in top]})
    return get_event(db, event.id)


def unpublish(db: sqlite3.Connection, actor: User, event: Event) -> Event:
    policy.require_organizer(db, actor, event.id)
    with transaction(db):
        db.execute("UPDATE events SET results_published_at = NULL, updated_at = ? WHERE id = ?", (clock.now_iso(), event.id))
        audit.record(db, "results.unpublish", actor=actor, event_id=event.id, target_type="event", target_id=event.id)
    return get_event(db, event.id)


def serialize(table: ResultsTable, *, include_methods: bool) -> dict[str, Any]:
    keep = ["rank", "id", "title", "team_name", "track_name", "reviews", "score"]
    if include_methods:
        keep += ["movement"] + [f"{m}{suffix}" for m in METHODS for suffix in ("", "_rank")]
    return {
        "event": {"id": table.event.id, "name": table.event.name},
        "method": table.method,
        "method_label": normalization.METHOD_LABELS[table.method],
        "published_at": table.event.results_published_at,
        "results": [{k: r.get(k) for k in keep} for r in table.rows],
    }
