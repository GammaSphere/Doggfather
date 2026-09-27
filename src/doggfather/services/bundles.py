"""Bulk import of an event bundle (fixtures.json is one).

The bundle format is a superset of the DOGFOOD fixture shape: everything the
fixtures carry, plus optional fields (dates, prizes, criteria, tags, pending
assignments) that a full export also includes. Import is all-or-nothing: it
runs in one transaction and either lands completely or not at all.

Ids from the bundle are kept verbatim so an export can be re-imported and
diffed. People are matched by email, so importing a second event reuses the
accounts that already exist.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .. import audit, clock
from ..auth import User, ensure_user
from ..db import fetch_one, new_id, transaction
from ..errors import Conflict, ValidationFailed
from ..security import new_token

SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ImportReport:
    event_id: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return ", ".join(f"{v} {k}" for k, v in self.counts.items())


def slugify(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")[:60] or "event"


def unique_slug(db: sqlite3.Connection, base: str) -> str:
    slug, n = base, 2
    while fetch_one(db, "SELECT 1 FROM events WHERE slug = ?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def name_from_email(email: str) -> str:
    local = email.split("@")[0]
    parts = re.findall(r"[A-Za-z]+|\d+", local)
    words = [p for p in parts if p.isalpha()]
    numbers = [p for p in parts if p.isdigit()]
    if not words:
        return local
    if words[0].lower() == "member" and numbers:
        return "Member " + ".".join(numbers)
    return words[0].capitalize()


def _link(value: Any, where: str, report: "ImportReport") -> str:
    """Keep only http(s) links; anything else is dropped with a warning."""
    text = str(value or "").strip()
    if text and not text.lower().startswith(("http://", "https://")):
        report.warnings.append(f"{where}: dropped non-http link {text[:40]!r}")
        return ""
    return text[:500]


def _require(data: dict, key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ValidationFailed(f"Bundle is missing {where}.{key}.", fields={f"{where}.{key}": "required"})
    return data[key]


def _ts(value: Any, where: str) -> str:
    try:
        return clock.normalize(str(value))
    except ValueError:
        raise ValidationFailed(f"{where} is not an ISO 8601 timestamp: {value!r}", fields={where: "bad timestamp"}) from None


def import_bundle(db: sqlite3.Connection, data: dict[str, Any], *, actor: User | None = None,
                  overrides: dict[str, Any] | None = None) -> ImportReport:
    if not isinstance(data, dict) or not isinstance(data.get("event"), dict):
        raise ValidationFailed("A bundle is a JSON object with an 'event' object.")
    ev = {**data["event"], **(overrides or {})}
    event_id = str(_require(ev, "id", "event"))
    name = str(_require(ev, "name", "event"))
    close = _ts(_require(ev, "submissions_close", "event"), "event.submissions_close")
    close_dt = clock.parse(close)
    opens = _ts(ev.get("submissions_open") or clock.iso(close_dt - timedelta(hours=72)), "event.submissions_open")
    judging_close = _ts(ev.get("judging_close") or clock.iso(close_dt + timedelta(days=10)), "event.judging_close")

    report = ImportReport(event_id=event_id)
    counts = report.counts
    now = clock.now_iso()

    with transaction(db):
        if fetch_one(db, "SELECT 1 FROM events WHERE id = ?", (event_id,)):
            raise Conflict(f"An event with id {event_id} already exists.", code="event_exists")
        slug = unique_slug(db, slugify(str(ev.get("slug") or name)))
        db.execute(
            "INSERT INTO events (id, slug, name, tagline, description, submissions_open_at, submissions_close_at,"
            " judging_close_at, max_team_size, review_target, normalization, results_published_at, voting_mode,"
            " voting_open_at, voting_close_at, vote_credits, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, slug, name, ev.get("tagline", ""), ev.get("description", ""), opens, close, judging_close,
             int(ev.get("max_team_size", 4)), int(ev.get("review_target", 3)), ev.get("normalization", "bias"),
             _ts(ev["results_published_at"], "event.results_published_at") if ev.get("results_published_at") else None,
             ev.get("voting_mode", "off"),
             _ts(ev["voting_open"], "event.voting_open") if ev.get("voting_open") else None,
             _ts(ev["voting_close"], "event.voting_close") if ev.get("voting_close") else None,
             int(ev.get("vote_credits", 25)), actor.id if actor else None, now, now),
        )
        if actor is not None:
            db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'organizer', ?)", (event_id, actor.id, now))

        # ---- tracks and prizes
        track_ids: set[str] = set()
        for pos, track in enumerate(data.get("tracks") or []):
            tid = str(_require(track, "id", "tracks[]"))
            db.execute("INSERT INTO tracks (id, event_id, name, description, position) VALUES (?, ?, ?, ?, ?)",
                       (tid, event_id, _require(track, "name", "tracks[]"), track.get("description", ""), pos))
            track_ids.add(tid)
        counts["tracks"] = len(track_ids)

        for pos, prize in enumerate(data.get("prizes") or []):
            db.execute(
                "INSERT INTO prizes (id, event_id, track_id, name, value, description, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (prize.get("id") or new_id("prz"), event_id, prize.get("track") if prize.get("track") in track_ids else None,
                 _require(prize, "name", "prizes[]"), str(prize.get("value", "")), prize.get("description", ""), pos),
            )
        counts["prizes"] = len(data.get("prizes") or [])

        for pos, q in enumerate(data.get("questions") or []):
            db.execute(
                "INSERT INTO custom_questions (id, event_id, prompt, help, kind, options, required, position)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (q.get("id") or new_id("q"), event_id, _require(q, "prompt", "questions[]"), q.get("help", ""),
                 q.get("kind", "text"), json.dumps(q.get("options", [])), int(bool(q.get("required"))), pos),
            )

        # ---- rubric: explicit criteria, else derived from the score keys
        criteria = data.get("criteria")
        if not criteria:
            keys: list[str] = []
            for score in data.get("scores") or []:
                for key in (score.get("criteria") or {}):
                    if key not in keys:
                        keys.append(key)
            criteria = [{"key": k, "label": k.replace("_", " ").capitalize(), "weight": 1} for k in keys]
        criterion_ids: dict[str, str] = {}
        for pos, c in enumerate(criteria):
            key = str(_require(c, "key", "criteria[]"))
            cid = c.get("id") or f"{event_id}:{key}"
            db.execute(
                "INSERT INTO criteria (id, event_id, key, label, description, weight, min_score, max_score, position)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, event_id, key, c.get("label") or key.capitalize(), c.get("description", ""),
                 float(c.get("weight", 1)), int(c.get("min", 1)), int(c.get("max", 5)), pos),
            )
            criterion_ids[key] = cid
            for track_id, weight in (c.get("track_weights") or {}).items():
                if track_id in track_ids:
                    db.execute("INSERT INTO criterion_track_weights VALUES (?, ?, ?)", (cid, track_id, float(weight)))
        counts["criteria"] = len(criterion_ids)

        # ---- organizers listed in the bundle
        for org in data.get("organizers") or []:
            person = ensure_user(db, _require(org, "email", "organizers[]"), org.get("name", ""))
            db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'organizer', ?)", (event_id, person.id, now))

        # ---- judges
        judge_ids: dict[str, str] = {}
        for judge in data.get("judges") or []:
            jid = str(_require(judge, "id", "judges[]"))
            person = ensure_user(db, _require(judge, "email", "judges[]"), judge.get("name", ""), user_id=jid)
            judge_ids[jid] = person.id
            db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'judge', ?)", (event_id, person.id, now))
            for tid in judge.get("tracks") or []:
                if tid not in track_ids:
                    report.warnings.append(f"judge {jid} lists unknown track {tid}")
                    continue
                db.execute("INSERT OR IGNORE INTO judge_tracks VALUES (?, ?, ?)", (event_id, person.id, tid))
        counts["judges"] = len(judge_ids)

        # ---- teams and members
        team_ids: set[str] = set()
        members_total = 0
        for team in data.get("teams") or []:
            tid = str(_require(team, "id", "teams[]"))
            db.execute("INSERT INTO teams (id, event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?, ?)",
                       (tid, event_id, _require(team, "name", "teams[]"), new_token(9), now))
            team_ids.add(tid)
            for idx, member in enumerate(team.get("members") or []):
                email = member if isinstance(member, str) else member.get("email")
                person = ensure_user(db, email, name_from_email(email) if isinstance(member, str) else member.get("name", ""))
                try:
                    with transaction(db):
                        db.execute("INSERT INTO event_members VALUES (?, ?, 'participant', ?)", (event_id, person.id, now))
                        db.execute("INSERT INTO team_members (team_id, event_id, user_id, role, joined_at) VALUES (?, ?, ?, ?, ?)",
                                   (tid, event_id, person.id, "captain" if idx == 0 else "member", now))
                    members_total += 1
                except sqlite3.IntegrityError as exc:
                    report.warnings.append(f"skipped {email} in team {tid}: {exc}")
        counts["teams"] = len(team_ids)
        counts["participants"] = members_total

        # ---- projects
        project_ids: set[str] = set()
        for p in data.get("projects") or []:
            pid = str(_require(p, "id", "projects[]"))
            team_id = str(_require(p, "team", "projects[]"))
            if team_id not in team_ids:
                raise ValidationFailed(f"Project {pid} references unknown team {team_id}.")
            track = p.get("track") if p.get("track") in track_ids else None
            submitted_at = _ts(p["submitted_at"], f"projects[{pid}].submitted_at") if p.get("submitted_at") else None
            status = p.get("status") or ("submitted" if submitted_at else "draft")
            summary = p.get("tagline") or p.get("summary") or ""
            created = submitted_at or now
            db.execute(
                "INSERT INTO projects (id, event_id, team_id, track_id, title, tagline, description, video_url, repo_url,"
                " demo_url, status, submitted_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, event_id, team_id, track, _require(p, "title", "projects[]"), summary,
                 p.get("description") or summary, _link(p.get("video_url"), pid, report),
                 _link(p.get("repo_url"), pid, report), _link(p.get("demo_url"), pid, report),
                 status, submitted_at, p.get("created_at") or created, p.get("updated_at") or created),
            )
            for tag in p.get("tags") or []:
                db.execute("INSERT OR IGNORE INTO project_tags VALUES (?, ?)", (pid, str(tag).lower()[:24]))
            project_ids.add(pid)
        counts["projects"] = len(project_ids)

        # ---- judging: scores imply completed assignments
        scored = 0
        for s in data.get("scores") or []:
            jid, pid = judge_ids.get(s.get("judge")), s.get("project")
            if jid is None or pid not in project_ids:
                report.warnings.append(f"score {s.get('judge')}->{pid} references unknown judge or project")
                continue
            assignment_id = new_id("asg")
            submitted = _ts(s["submitted_at"], "scores[].submitted_at") if s.get("submitted_at") else close
            db.execute(
                "INSERT INTO assignments (id, event_id, judge_id, project_id, batch, status, assigned_at)"
                " VALUES (?, ?, ?, ?, ?, 'done', ?)",
                (assignment_id, event_id, jid, pid, s.get("batch", "imported"), close),
            )
            score_id = new_id("scr")
            db.execute(
                "INSERT INTO scores (id, assignment_id, event_id, judge_id, project_id, comment, submitted_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (score_id, assignment_id, event_id, jid, pid, s.get("comment") or "", submitted, submitted),
            )
            for key, value in (s.get("criteria") or {}).items():
                if key not in criterion_ids:
                    raise ValidationFailed(f"Score uses criterion '{key}' that the rubric does not define.")
                db.execute("INSERT INTO score_items VALUES (?, ?, ?)", (score_id, criterion_ids[key], int(value)))
            scored += 1
        counts["scores"] = scored

        pending = 0
        for a in data.get("assignments") or []:
            jid, pid = judge_ids.get(a.get("judge")), a.get("project")
            if jid is None or pid not in project_ids:
                continue
            cur = db.execute(
                "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, project_id, batch, status, assigned_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (a.get("id") or new_id("asg"), event_id, jid, pid, a.get("batch", "imported"), now),
            )
            pending += cur.rowcount
        if pending:
            counts["pending_assignments"] = pending

        audit.record(db, "import.bundle", actor=actor, event_id=event_id, target_type="event", target_id=event_id,
                     detail={"summary": report.summary()})
    return report
