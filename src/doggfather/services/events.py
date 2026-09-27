"""Events, their phases, tracks, prizes and custom submission questions."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from .. import audit, clock
from ..auth import User, ensure_user, validate_email
from ..db import fetch_all, fetch_one, fetch_value, new_id, transaction
from ..errors import Conflict, NotFound, ValidationFailed
from ..policy import require_organizer

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
QUESTION_KINDS = ("text", "textarea", "url", "choice")
VOTING_MODES = {
    "off": "No community vote",
    "account": "Logged-in accounts (strongest)",
    "email": "Verified email address (one-time code)",
    "link": "Anyone with the link (weakest; device + IP checks)",
}
DEFAULT_CRITERIA = (
    ("functionality", "Functionality", "Does it work end to end? Could someone use it tomorrow?"),
    ("quality", "Quality", "Is it built well: code, UX, docs, robustness?"),
    ("innovation", "Innovation", "Is there a new idea, or a known idea done notably better?"),
)


@dataclass(frozen=True)
class Event:
    id: str
    slug: str
    name: str
    tagline: str
    description: str
    submissions_open_at: str
    submissions_close_at: str
    judging_close_at: str
    max_team_size: int
    review_target: int
    normalization: str
    results_published_at: str | None
    created_at: str
    voting_mode: str = "off"
    voting_open_at: str | None = None
    voting_close_at: str | None = None
    vote_credits: int = 25

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict) -> "Event":
        keys = set(row.keys())
        return cls(**{f.name: row[f.name] for f in fields(cls) if f.name in keys})

    # --------------------------------------------------------------- phases
    def _now(self, now: datetime | None) -> str:
        return clock.iso(now) if now else clock.now_iso()

    def submissions_open(self, now: datetime | None = None) -> bool:
        t = self._now(now)
        return self.submissions_open_at <= t < self.submissions_close_at

    def submissions_closed(self, now: datetime | None = None) -> bool:
        return self._now(now) >= self.submissions_close_at

    def judging_open(self, now: datetime | None = None) -> bool:
        t = self._now(now)
        return self.submissions_close_at <= t < self.judging_close_at

    @property
    def results_published(self) -> bool:
        return self.results_published_at is not None

    @property
    def voting_enabled(self) -> bool:
        return self.voting_mode != "off" and bool(self.voting_open_at and self.voting_close_at)

    def voting_open(self, now: datetime | None = None) -> bool:
        t = self._now(now)
        return self.voting_enabled and self.voting_open_at <= t < self.voting_close_at  # type: ignore[operator]

    def voting_closed(self, now: datetime | None = None) -> bool:
        return self.voting_enabled and self._now(now) >= self.voting_close_at  # type: ignore[operator]

    @property
    def tally_public(self) -> bool:
        """Community tallies are hidden while voting runs, and stay hidden
        until the organizers publish results."""
        return self.results_published and (not self.voting_enabled or self.voting_closed())

    @property
    def phase(self) -> str:
        if self.results_published:
            return "results"
        t = clock.now_iso()
        if t < self.submissions_open_at:
            return "upcoming"
        if t < self.submissions_close_at:
            return "submissions"
        if t < self.judging_close_at:
            return "judging"
        return "closed"

    @property
    def next_deadline(self) -> tuple[str, str] | None:
        t = clock.now_iso()
        for label, when in (("Submissions open", self.submissions_open_at),
                            ("Submissions close", self.submissions_close_at),
                            ("Judging closes", self.judging_close_at)):
            if when and t < when:
                return label, when
        return None

    def timeline(self) -> list[dict[str, Any]]:
        t = clock.now_iso()
        steps = [
            ("Submissions open", self.submissions_open_at),
            ("Submissions close", self.submissions_close_at),
            ("Judging closes", self.judging_close_at),
        ]
        if self.voting_enabled:
            steps += [("Community voting opens", self.voting_open_at or ""),
                      ("Community voting closes", self.voting_close_at or "")]
            steps.sort(key=lambda step: step[1])
        steps.append(("Results published", self.results_published_at or ""))
        out, current_marked = [], False
        for label, when in steps:
            done = bool(when) and when <= t
            state = "done" if done else ("now" if not current_marked else "later")
            if not done:
                current_marked = True
            out.append({"label": label, "when": when, "state": state})
        return out


# ------------------------------------------------------------------ reads

def get_event(db: sqlite3.Connection, event_id: str) -> Event:
    row = fetch_one(db, "SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise NotFound("No such event.")
    return Event.from_row(row)


def get_event_by_slug(db: sqlite3.Connection, slug: str) -> Event:
    row = fetch_one(db, "SELECT * FROM events WHERE slug = ?", (slug,))
    if row is None:
        raise NotFound("No such event.")
    return Event.from_row(row)


def list_events(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = fetch_all(
        db,
        "SELECT e.*, (SELECT COUNT(*) FROM projects p WHERE p.event_id = e.id AND p.status = 'submitted') AS project_count"
        " FROM events e ORDER BY e.submissions_close_at DESC",
    )
    return [{"event": Event.from_row(r), "project_count": r["project_count"]} for r in rows]


def list_tracks(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT t.*, (SELECT COUNT(*) FROM projects p WHERE p.track_id = t.id AND p.status = 'submitted') AS project_count"
        " FROM tracks t WHERE t.event_id = ? ORDER BY t.position, t.name",
        (event_id,),
    )


def list_prizes(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT z.*, t.name AS track_name FROM prizes z LEFT JOIN tracks t ON t.id = z.track_id"
        " WHERE z.event_id = ? ORDER BY z.position, z.name",
        (event_id,),
    )


def list_questions(db: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(db, "SELECT * FROM custom_questions WHERE event_id = ? ORDER BY position, prompt", (event_id,))
    return [{**dict(r), "options": json.loads(r["options"] or "[]")} for r in rows]


def event_counts(db: sqlite3.Connection, event_id: str) -> dict[str, int]:
    def one(sql: str) -> int:
        return fetch_value(db, sql, (event_id,), default=0)

    return {
        "submitted": one("SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted'"),
        "drafts": one("SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'draft'"),
        "teams": one("SELECT COUNT(*) FROM teams WHERE event_id = ?"),
        "participants": one("SELECT COUNT(*) FROM team_members WHERE event_id = ?"),
        "judges": one("SELECT COUNT(*) FROM event_members WHERE event_id = ? AND role = 'judge'"),
        "tracks": one("SELECT COUNT(*) FROM tracks WHERE event_id = ?"),
        "assignments": one("SELECT COUNT(*) FROM assignments WHERE event_id = ?"),
        "reviews_done": one("SELECT COUNT(*) FROM assignments WHERE event_id = ? AND status = 'done'"),
    }


def organizers(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return fetch_all(
        db,
        "SELECT u.id, u.name, u.email FROM event_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.event_id = ? AND m.role = 'organizer' ORDER BY u.name",
        (event_id,),
    )


def can_create_events(db: sqlite3.Connection, user: User | None) -> bool:
    if user is None:
        return False
    if user.is_admin:
        return True
    return fetch_one(db, "SELECT 1 FROM event_members WHERE user_id = ? AND role = 'organizer'", (user.id,)) is not None


# ------------------------------------------------------------- validation

def _parse_when(raw: Any, label: str, errors: dict[str, str], key: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        errors[key] = f"{label} is required."
        return None
    try:
        return clock.normalize(text)
    except ValueError:
        errors[key] = f"{label} must be a date and time (UTC)."
        return None


def _int_in(raw: Any, lo: int, hi: int, key: str, errors: dict[str, str], default: int) -> int:
    try:
        value = int(str(raw).strip()) if str(raw or "").strip() else default
    except ValueError:
        errors[key] = "Enter a whole number."
        return default
    if not lo <= value <= hi:
        errors[key] = f"Must be between {lo} and {hi}."
    return value


def clean_event_values(values: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    name = str(values.get("name") or "").strip()
    if not 3 <= len(name) <= 120:
        errors["name"] = "Name must be 3 to 120 characters."
    slug = str(values.get("slug") or "").strip().lower() or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    if not SLUG_RE.match(slug):
        errors["slug"] = "Lowercase letters, digits and dashes only."
    opens = _parse_when(values.get("submissions_open_at"), "Opening time", errors, "submissions_open_at")
    closes = _parse_when(values.get("submissions_close_at"), "Deadline", errors, "submissions_close_at")
    judging = _parse_when(values.get("judging_close_at"), "Judging close", errors, "judging_close_at")
    if opens and closes and opens >= closes:
        errors["submissions_close_at"] = "The deadline must come after submissions open."
    if closes and judging and judging < closes:
        errors["judging_close_at"] = "Judging cannot close before submissions do."
    voting_mode = str(values.get("voting_mode") or "off")
    if voting_mode not in VOTING_MODES:
        errors["voting_mode"] = "Pick a voting mode."
        voting_mode = "off"
    voting_open = voting_close = None
    if voting_mode != "off":
        voting_open = _parse_when(values.get("voting_open_at"), "Voting opens", errors, "voting_open_at")
        voting_close = _parse_when(values.get("voting_close_at"), "Voting closes", errors, "voting_close_at")
        if voting_open and voting_close and voting_open >= voting_close:
            errors["voting_close_at"] = "Voting must close after it opens."
    else:
        for key in ("voting_open_at", "voting_close_at"):
            raw = str(values.get(key) or "").strip()
            if raw:
                try:
                    parsed = clock.normalize(raw)
                except ValueError:
                    parsed = None
                if key == "voting_open_at":
                    voting_open = parsed
                else:
                    voting_close = parsed
    clean = {
        "name": name,
        "slug": slug,
        "tagline": str(values.get("tagline") or "").strip()[:200],
        "description": str(values.get("description") or "").strip()[:20000],
        "submissions_open_at": opens,
        "submissions_close_at": closes,
        "judging_close_at": judging,
        "max_team_size": _int_in(values.get("max_team_size"), 1, 50, "max_team_size", errors, 4),
        "review_target": _int_in(values.get("review_target"), 1, 20, "review_target", errors, 3),
        "voting_mode": voting_mode,
        "voting_open_at": voting_open,
        "voting_close_at": voting_close,
        "vote_credits": _int_in(values.get("vote_credits"), 1, 10000, "vote_credits", errors, 25),
    }
    if errors:
        raise ValidationFailed(fields=errors)
    return clean


# ----------------------------------------------------------------- writes

def create_event(db: sqlite3.Connection, actor: User, values: dict[str, Any]) -> Event:
    clean = clean_event_values(values)
    event_id = new_id("evt")
    now = clock.now_iso()
    with transaction(db):
        if fetch_one(db, "SELECT 1 FROM events WHERE slug = ?", (clean["slug"],)):
            raise ValidationFailed(fields={"slug": "That address is taken."})
        db.execute(
            "INSERT INTO events (id, slug, name, tagline, description, submissions_open_at, submissions_close_at,"
            " judging_close_at, max_team_size, review_target, voting_mode, voting_open_at, voting_close_at,"
            " vote_credits, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, clean["slug"], clean["name"], clean["tagline"], clean["description"],
             clean["submissions_open_at"], clean["submissions_close_at"], clean["judging_close_at"],
             clean["max_team_size"], clean["review_target"], clean["voting_mode"], clean["voting_open_at"],
             clean["voting_close_at"], clean["vote_credits"], actor.id, now, now),
        )
        db.execute("INSERT INTO event_members VALUES (?, ?, 'organizer', ?)", (event_id, actor.id, now))
        for pos, (key, label, description) in enumerate(DEFAULT_CRITERIA):
            db.execute(
                "INSERT INTO criteria (id, event_id, key, label, description, weight, position) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (f"{event_id}:{key}", event_id, key, label, description, pos),
            )
        audit.record(db, "event.create", actor=actor, event_id=event_id, target_type="event", target_id=event_id,
                     detail={"name": clean["name"]})
    return get_event(db, event_id)


EDITABLE = ("name", "slug", "tagline", "description", "submissions_open_at", "submissions_close_at",
            "judging_close_at", "max_team_size", "review_target", "voting_mode", "voting_open_at",
            "voting_close_at", "vote_credits")


def update_event(db: sqlite3.Connection, actor: User, event: Event, values: dict[str, Any]) -> Event:
    require_organizer(db, actor, event.id)
    clean = clean_event_values(values)
    changed = [k for k in EDITABLE if str(getattr(event, k)) != str(clean[k])]
    if not changed:
        return event
    with transaction(db):
        if "slug" in changed and fetch_one(db, "SELECT 1 FROM events WHERE slug = ? AND id <> ?", (clean["slug"], event.id)):
            raise ValidationFailed(fields={"slug": "That address is taken."})
        assignments = ", ".join(f"{k} = ?" for k in changed)
        db.execute(f"UPDATE events SET {assignments}, updated_at = ? WHERE id = ?",
                   (*[clean[k] for k in changed], clock.now_iso(), event.id))
        audit.record(db, "event.update", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"fields": changed, **{f"new_{k}": clean[k] for k in changed if k.endswith("_at")}})
    return get_event(db, event.id)


PHASE_ACTIONS = {
    "open_submissions": "Submissions opened now",
    "close_submissions": "Submissions closed now",
    "close_judging": "Judging closed now",
    "extend_judging": "Judging extended by 7 days",
}


def apply_phase_action(db: sqlite3.Connection, actor: User, event: Event, action: str) -> Event:
    """Organizer shortcuts that move a phase boundary to 'now'. Each is an
    ordinary, audited date change, so the rules stay in one place."""
    require_organizer(db, actor, event.id)
    now = clock.now()
    t = clock.iso(now)
    before = clock.iso(now - timedelta(minutes=1))
    if action == "open_submissions":
        if event.results_published_at:
            raise Conflict("Unpublish results before reopening submissions.")
        close = event.submissions_close_at if event.submissions_close_at > t else clock.iso(now + timedelta(days=2))
        judging = event.judging_close_at
        if judging < close:
            judging = clock.iso(clock.parse(close) + timedelta(days=7))
        updates = {"submissions_open_at": t, "submissions_close_at": close, "judging_close_at": judging}
    elif action == "close_submissions":
        updates = {"submissions_close_at": t}
        if event.submissions_open_at >= t:
            updates["submissions_open_at"] = before
        if event.judging_close_at < t:
            updates["judging_close_at"] = t
    elif action == "close_judging":
        if not event.submissions_closed():
            raise Conflict("Close submissions before closing judging.")
        updates = {"judging_close_at": t}
    elif action == "extend_judging":
        if not event.submissions_closed():
            raise Conflict("Judging starts when submissions close.")
        updates = {"judging_close_at": clock.iso(max(now, clock.parse(event.judging_close_at)) + timedelta(days=7))}
    else:
        raise ValidationFailed(f"Unknown phase action {action!r}.")
    with transaction(db):
        assignments = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE events SET {assignments}, updated_at = ? WHERE id = ?", (*updates.values(), t, event.id))
        audit.record(db, "event.phase", actor=actor, event_id=event.id, target_type="event", target_id=event.id,
                     detail={"what": PHASE_ACTIONS[action].lower(), "when": t, **updates})
    return get_event(db, event.id)


def add_track(db: sqlite3.Connection, actor: User, event: Event, name: str, description: str = "") -> str:
    require_organizer(db, actor, event.id)
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ValidationFailed(fields={"track_name": "Track names are 1 to 80 characters."})
    if fetch_one(db, "SELECT 1 FROM tracks WHERE event_id = ? AND name = ?", (event.id, name)):
        raise ValidationFailed(fields={"track_name": "This event already has that track."})
    track_id = new_id("trk")
    position = fetch_value(db, "SELECT COALESCE(MAX(position), -1) + 1 FROM tracks WHERE event_id = ?", (event.id,))
    with transaction(db):
        db.execute("INSERT INTO tracks (id, event_id, name, description, position) VALUES (?, ?, ?, ?, ?)",
                   (track_id, event.id, name, description.strip()[:500], position))
        audit.record(db, "track.add", actor=actor, event_id=event.id, target_type="track", target_id=track_id,
                     detail={"name": name})
    return track_id


def remove_track(db: sqlite3.Connection, actor: User, event: Event, track_id: str) -> None:
    require_organizer(db, actor, event.id)
    track = fetch_one(db, "SELECT * FROM tracks WHERE id = ? AND event_id = ?", (track_id, event.id))
    if track is None:
        raise NotFound("No such track.")
    if fetch_one(db, "SELECT 1 FROM projects WHERE track_id = ?", (track_id,)):
        raise Conflict("Projects are entered in this track. Move them before removing it.")
    with transaction(db):
        db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        audit.record(db, "track.remove", actor=actor, event_id=event.id, target_type="track", target_id=track_id,
                     detail={"name": track["name"]})


def add_prize(db: sqlite3.Connection, actor: User, event: Event, name: str, value: str = "",
              description: str = "", track_id: str | None = None) -> str:
    require_organizer(db, actor, event.id)
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValidationFailed(fields={"prize_name": "Prize names are 1 to 120 characters."})
    if track_id and not fetch_one(db, "SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (track_id, event.id)):
        raise ValidationFailed(fields={"prize_track": "Pick a track from this event."})
    prize_id = new_id("prz")
    position = fetch_value(db, "SELECT COALESCE(MAX(position), -1) + 1 FROM prizes WHERE event_id = ?", (event.id,))
    with transaction(db):
        db.execute(
            "INSERT INTO prizes (id, event_id, track_id, name, value, description, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (prize_id, event.id, track_id or None, name, value.strip()[:60], description.strip()[:500], position),
        )
        audit.record(db, "prize.add", actor=actor, event_id=event.id, target_type="prize", target_id=prize_id,
                     detail={"name": name})
    return prize_id


def remove_prize(db: sqlite3.Connection, actor: User, event: Event, prize_id: str) -> None:
    require_organizer(db, actor, event.id)
    prize = fetch_one(db, "SELECT * FROM prizes WHERE id = ? AND event_id = ?", (prize_id, event.id))
    if prize is None:
        raise NotFound("No such prize.")
    with transaction(db):
        db.execute("DELETE FROM prizes WHERE id = ?", (prize_id,))
        audit.record(db, "prize.remove", actor=actor, event_id=event.id, target_type="prize", target_id=prize_id,
                     detail={"name": prize["name"]})


def add_question(db: sqlite3.Connection, actor: User, event: Event, prompt: str, kind: str = "text",
                 required: bool = False, options: list[str] | None = None, help_text: str = "") -> str:
    require_organizer(db, actor, event.id)
    prompt = prompt.strip()
    errors = {}
    if not 3 <= len(prompt) <= 300:
        errors["question_prompt"] = "Questions are 3 to 300 characters."
    if kind not in QUESTION_KINDS:
        errors["question_kind"] = "Unknown question type."
    options = [o.strip() for o in (options or []) if o.strip()]
    if kind == "choice" and len(options) < 2:
        errors["question_options"] = "A choice question needs at least two options."
    if errors:
        raise ValidationFailed(fields=errors)
    question_id = new_id("q")
    position = fetch_value(db, "SELECT COALESCE(MAX(position), -1) + 1 FROM custom_questions WHERE event_id = ?", (event.id,))
    with transaction(db):
        db.execute(
            "INSERT INTO custom_questions (id, event_id, prompt, help, kind, options, required, position)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (question_id, event.id, prompt, help_text.strip()[:300], kind, json.dumps(options), int(required), position),
        )
        audit.record(db, "question.add", actor=actor, event_id=event.id, target_type="question",
                     target_id=question_id, detail={"prompt": prompt})
    return question_id


def remove_question(db: sqlite3.Connection, actor: User, event: Event, question_id: str) -> None:
    require_organizer(db, actor, event.id)
    question = fetch_one(db, "SELECT * FROM custom_questions WHERE id = ? AND event_id = ?", (question_id, event.id))
    if question is None:
        raise NotFound("No such question.")
    with transaction(db):
        db.execute("DELETE FROM custom_questions WHERE id = ?", (question_id,))
        audit.record(db, "question.remove", actor=actor, event_id=event.id, target_type="question",
                     target_id=question_id, detail={"prompt": question["prompt"]})


def add_organizer(db: sqlite3.Connection, actor: User, event: Event, email: str, name: str = "") -> User:
    require_organizer(db, actor, event.id)
    error = validate_email(email.strip().lower())
    if error:
        raise ValidationFailed(fields={"organizer_email": error})
    with transaction(db):
        person = ensure_user(db, email, name or email.split("@")[0])
        if fetch_one(db, "SELECT 1 FROM event_members WHERE event_id = ? AND user_id = ? AND role IN ('judge', 'participant')",
                     (event.id, person.id)):
            raise Conflict("That person judges or competes in this event, so they cannot organize it.")
        db.execute("INSERT OR IGNORE INTO event_members VALUES (?, ?, 'organizer', ?)", (event.id, person.id, clock.now_iso()))
        audit.record(db, "event.organizer_add", actor=actor, event_id=event.id, target_type="user", target_id=person.id,
                     detail={"email": person.email})
    return person
