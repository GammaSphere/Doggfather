"""Weighted scoring rubrics.

Scores are stored raw, one integer per criterion, and weights are applied
only when results are computed. An organizer can therefore re-weight at any
time without touching a single stored score, and the audit log records the
change. What cannot change once scoring has begun is the *scale* (min/max)
or the set of criteria, because that would make stored scores ambiguous.

Weighted total for one scorecard, on the rubric's own scale:

    total = sum(w_c * s_c) / sum(w_c)      over criteria c with w_c > 0

Per-track overrides replace the event weight for projects in that track.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import audit
from ..auth import User
from ..db import fetch_all, fetch_one, fetch_value, transaction
from ..errors import Conflict, ValidationFailed
from ..policy import require_organizer
from .events import Event

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def list_criteria(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(db, "SELECT * FROM criteria WHERE event_id = ? ORDER BY position, key", (event_id,))


def track_overrides(db: sqlite3.Connection, event_id: str) -> dict[str, dict[str, float]]:
    rows = fetch_all(
        db,
        "SELECT w.criterion_id, w.track_id, w.weight FROM criterion_track_weights w"
        " JOIN criteria c ON c.id = w.criterion_id WHERE c.event_id = ?",
        (event_id,),
    )
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["track_id"], {})[row["criterion_id"]] = row["weight"]
    return out


def weight_table(db: sqlite3.Connection, event_id: str) -> dict[str | None, dict[str, float]]:
    """Effective weights: key None is the event default, then one entry per
    track that overrides at least one criterion."""
    base = {c["id"]: c["weight"] for c in list_criteria(db, event_id)}
    table: dict[str | None, dict[str, float]] = {None: base}
    for track_id, overrides in track_overrides(db, event_id).items():
        table[track_id] = {**base, **overrides}
    return table


def weights_for(table: dict[str | None, dict[str, float]], track_id: str | None) -> dict[str, float]:
    return table.get(track_id) or table[None]


def weighted_total(values: dict[str, float], weights: dict[str, float]) -> float | None:
    used = [(weights.get(cid, 0.0), v) for cid, v in values.items() if weights.get(cid, 0.0) > 0]
    total_weight = sum(w for w, _ in used)
    if total_weight <= 0:
        return (sum(values.values()) / len(values)) if values else None
    return sum(w * v for w, v in used) / total_weight


def scoring_started(db: sqlite3.Connection, event_id: str) -> bool:
    return fetch_value(db, "SELECT COUNT(*) FROM scores WHERE event_id = ?", (event_id,), default=0) > 0


def _num(raw: Any, key: str, errors: dict[str, str], lo: float, hi: float, default: float) -> float:
    text = str(raw if raw is not None else "").strip()
    if text == "":
        return default
    try:
        value = float(text)
    except ValueError:
        errors[key] = "Enter a number."
        return default
    if not lo <= value <= hi:
        errors[key] = f"Between {lo:g} and {hi:g}."
    return value


def update_rubric(db: sqlite3.Connection, actor: User, event: Event, rows: list[dict[str, Any]],
                  overrides: dict[tuple[str, str], Any] | None = None) -> list[str]:
    """Apply an edited rubric. ``rows`` holds existing criteria (with ``id``)
    and new ones (without); ``delete`` marks removals. ``overrides`` maps
    (criterion_id, track_id) to a weight, or blank to inherit."""
    require_organizer(db, actor, event.id)
    locked = scoring_started(db, event.id)
    current = {c["id"]: c for c in list_criteria(db, event.id)}
    errors: dict[str, str] = {}
    changes: list[str] = []
    keep = [r for r in rows if not r.get("delete")]
    if not keep:
        raise ValidationFailed(fields={"rubric": "A rubric needs at least one criterion."})

    seen_keys: set[str] = set()
    with transaction(db):
        for pos, row in enumerate(rows):
            cid = row.get("id")
            prefix = cid or f"new{pos}"
            label = str(row.get("label") or "").strip()
            if row.get("delete"):
                if cid in current:
                    if locked:
                        raise Conflict("Scoring has started; criteria can no longer be removed.")
                    db.execute("DELETE FROM criteria WHERE id = ?", (cid,))
                    changes.append(f"removed {current[cid]['label']}")
                continue
            if not label:
                if cid:
                    errors[f"{prefix}.label"] = "Give the criterion a name."
                continue  # blank "new criterion" row
            weight = _num(row.get("weight"), f"{prefix}.weight", errors, 0, 100, 1.0)
            lo = int(_num(row.get("min_score"), f"{prefix}.min_score", errors, 0, 100, 1))
            hi = int(_num(row.get("max_score"), f"{prefix}.max_score", errors, 1, 100, 5))
            if lo >= hi:
                errors[f"{prefix}.max_score"] = "Max must exceed min."
            description = str(row.get("description") or "").strip()[:300]
            if cid in current:
                old = current[cid]
                if locked and (lo != old["min_score"] or hi != old["max_score"]):
                    raise Conflict("Scoring has started; the score range can no longer change.")
                seen_keys.add(old["key"])
                if (label, description, weight, lo, hi, pos) != (old["label"], old["description"], old["weight"],
                                                                old["min_score"], old["max_score"], old["position"]):
                    db.execute("UPDATE criteria SET label = ?, description = ?, weight = ?, min_score = ?,"
                               " max_score = ?, position = ? WHERE id = ?", (label, description, weight, lo, hi, pos, cid))
                    if weight != old["weight"]:
                        changes.append(f"{label} weight {old['weight']:g}→{weight:g}")
                    elif label != old["label"]:
                        changes.append(f"renamed {old['label']}→{label}")
            else:
                if locked:
                    raise Conflict("Scoring has started; new criteria can no longer be added.")
                key = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")[:32] or f"c{pos}"
                if not KEY_RE.match(key):
                    key = f"c_{key}"[:32]
                while key in seen_keys or fetch_one(db, "SELECT 1 FROM criteria WHERE event_id = ? AND key = ?", (event.id, key)):
                    key = f"{key[:28]}_{pos}"
                seen_keys.add(key)
                db.execute(
                    "INSERT INTO criteria (id, event_id, key, label, description, weight, min_score, max_score, position)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{event.id}:{key}", event.id, key, label, description, weight, lo, hi, pos),
                )
                changes.append(f"added {label}")

        for (cid, track_id), raw in (overrides or {}).items():
            existing = fetch_value(db, "SELECT weight FROM criterion_track_weights WHERE criterion_id = ? AND track_id = ?",
                                   (cid, track_id))
            text = str(raw if raw is not None else "").strip()
            if text == "":
                if existing is not None:
                    db.execute("DELETE FROM criterion_track_weights WHERE criterion_id = ? AND track_id = ?", (cid, track_id))
                    changes.append("cleared a track override")
                continue
            value = _num(text, f"override.{cid}.{track_id}", errors, 0, 100, 1.0)
            if existing != value:
                db.execute("INSERT INTO criterion_track_weights VALUES (?, ?, ?) ON CONFLICT (criterion_id, track_id)"
                           " DO UPDATE SET weight = excluded.weight", (cid, track_id, value))
                changes.append("set a track override")

        if errors:
            raise ValidationFailed(fields=errors)
        if changes:
            audit.record(db, "rubric.update", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                         detail={"summary": "; ".join(changes)})
    return changes
