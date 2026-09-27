"""Project submissions: draft, edit, submit, withdraw, and the deadline.

Deadline enforcement is one function, ``assert_submissions_open``, and it
guards every path that creates or changes submission content: HTML forms,
the JSON API, uploads and deletes. It compares the server clock to the
event's stored cutoff. Nothing from the client (a hidden form field, a
timestamp in a JSON body) is consulted. Refused late writes are audited, so
"cutoff gaming" leaves a trail.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import UploadFile

from .. import audit, clock
from ..auth import User
from ..config import Settings
from ..db import fetch_all, fetch_one, fetch_value, new_id, transaction
from ..errors import Conflict, Forbidden, NotFound, SubmissionsClosed, ValidationFailed
from . import duplicates, uploads, webhooks
from .events import Event, get_event, list_questions, list_tracks
from .teams import team_for_user

MAX_TAGS = 12
MAX_IMAGES = 8
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9+#.\-]{0,23}$")
URL_FIELDS = ("video_url", "repo_url", "demo_url")
TEXT_LIMITS = {"title": 80, "tagline": 140, "description": 20000}


# ------------------------------------------------------------------ reads

def get_project(db: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = fetch_one(db, "SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise NotFound("No such project.")
    return row


def project_for_team(db: sqlite3.Connection, team_id: str) -> sqlite3.Row | None:
    return fetch_one(db, "SELECT * FROM projects WHERE team_id = ? AND status <> 'withdrawn'"
                         " ORDER BY status = 'submitted' DESC, updated_at DESC LIMIT 1", (team_id,))


def tags_for(db: sqlite3.Connection, project_id: str) -> list[str]:
    return [r["tag"] for r in fetch_all(db, "SELECT tag FROM project_tags WHERE project_id = ? ORDER BY tag", (project_id,))]


def project_detail(db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = fetch_one(
        db,
        "SELECT p.*, tm.name AS team_name, t.name AS track_name FROM projects p JOIN teams tm ON tm.id = p.team_id"
        " LEFT JOIN tracks t ON t.id = p.track_id WHERE p.id = ?",
        (project_id,),
    )
    if row is None:
        raise NotFound("No such project.")
    answers = fetch_all(
        db,
        "SELECT q.id, q.prompt, q.kind, a.answer FROM custom_questions q LEFT JOIN project_answers a"
        " ON a.question_id = q.id AND a.project_id = ? WHERE q.event_id = ? ORDER BY q.position",
        (project_id, row["event_id"]),
    )
    return {
        **dict(row),
        "tags": tags_for(db, project_id),
        "images": fetch_all(db, "SELECT * FROM project_images WHERE project_id = ? ORDER BY position, id", (project_id,)),
        "answers": answers,
        "members": fetch_all(db, "SELECT u.id, u.name FROM team_members m JOIN users u ON u.id = m.user_id"
                                 " WHERE m.team_id = ? ORDER BY m.role = 'captain' DESC, m.joined_at", (row["team_id"],)),
    }


def is_team_member(db: sqlite3.Connection, user: User | None, project: sqlite3.Row) -> bool:
    if user is None:
        return False
    return fetch_one(db, "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                     (project["team_id"], user.id)) is not None


def require_editor(db: sqlite3.Connection, user: User | None, project: sqlite3.Row) -> User:
    if user is None or not is_team_member(db, user, project):
        raise Forbidden("Only the project's team can change it.")
    return user


# -------------------------------------------------------------- deadline

def assert_submissions_open(db: sqlite3.Connection, event: Event, *, actor: User | None = None,
                            title: str = "", project_id: str | None = None) -> None:
    if event.submissions_open():
        return
    if event.submissions_closed():
        reason = f"submissions closed {event.submissions_close_at}"
        exc: Exception = SubmissionsClosed(closed_at=event.submissions_close_at)
    else:
        reason = f"submissions open {event.submissions_open_at}"
        exc = SubmissionsClosed("Submissions for this event have not opened yet.", opens_at=event.submissions_open_at)
    audit.record(db, "project.refused", actor=actor, event_id=event.id, target_type="project", target_id=project_id,
                 detail={"title": title or "a new project", "reason": reason})
    raise exc


# ------------------------------------------------------------- validation

def _clean_url(value: str, field: str, errors: dict[str, str]) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(value) > 500:
        errors[field] = "Use a full http(s):// link."
    return value


def clean_tags(raw: Any) -> tuple[list[str], str | None]:
    items = raw if isinstance(raw, list) else str(raw or "").replace("#", " ").replace(",", " ").split()
    tags: list[str] = []
    for item in items:
        tag = str(item).strip().lower()
        if tag and tag not in tags:
            tags.append(tag)
    bad = [t for t in tags if not TAG_RE.match(t)]
    if bad:
        return tags, f"Tags are short words: letters, digits, + # . - (not '{bad[0]}')."
    if len(tags) > MAX_TAGS:
        return tags, f"Use at most {MAX_TAGS} tags."
    return tags, None


def clean_submission(db: sqlite3.Connection, event: Event, payload) -> tuple[dict[str, str], list[str] | None, dict[str, str]]:
    """Validate the editable fields present in ``payload`` (a deps.Payload).

    Returns (values, tags or None if absent, answers). Absent keys are left
    untouched so the JSON API can send partial updates.
    """
    errors: dict[str, str] = {}
    values: dict[str, str] = {}
    keys = set(payload.keys())
    # "summary" is the fixture/checker word for the one-line tagline.
    sources = {"tagline": "tagline" if "tagline" in keys else "summary"}
    for field, limit in TEXT_LIMITS.items():
        source = sources.get(field, field)
        if source in keys:
            raw = payload.text(source)
            if len(raw) > limit:
                errors[field] = f"Keep it under {limit} characters."
            values[field] = raw
    if "title" in values and len(values["title"]) < 2:
        errors["title"] = "Give the project a name."
    for field in URL_FIELDS:
        if field in keys:
            values[field] = _clean_url(payload.text(field), field, errors)
    if "track_id" in keys:
        track_id = payload.text("track_id")
        if track_id and not fetch_one(db, "SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (track_id, event.id)):
            errors["track_id"] = "Pick one of this event's tracks."
        values["track_id"] = track_id
    tags = None
    if "tags" in keys:
        raw_tags = payload.getlist("tags") if payload.is_json else payload.text("tags")
        tags, tag_error = clean_tags(raw_tags)
        if tag_error:
            errors["tags"] = tag_error
    answers: dict[str, str] = {}
    for q in list_questions(db, event.id):
        key = f"q_{q['id']}"
        if key not in keys:
            continue
        answer = payload.text(key)
        if len(answer) > 5000:
            errors[key] = "Keep answers under 5000 characters."
        elif answer and q["kind"] == "url":
            _clean_url(answer, key, errors)
        elif answer and q["kind"] == "choice" and answer not in q["options"]:
            errors[key] = "Pick one of the listed options."
        answers[q["id"]] = answer
    if errors:
        raise ValidationFailed(fields=errors)
    return values, tags, answers


def missing_for_submission(db: sqlite3.Connection, project: sqlite3.Row) -> dict[str, str]:
    """What still blocks a draft from being submitted."""
    missing: dict[str, str] = {}
    if not project["title"].strip():
        missing["title"] = "A name is required."
    if not project["tagline"].strip():
        missing["tagline"] = "Add a one-line tagline."
    if len(project["description"].strip()) < 20:
        missing["description"] = "Describe the project in at least a couple of sentences."
    if not project["repo_url"]:
        missing["repo_url"] = "Link the source repository."
    if not project["track_id"] and list_tracks(db, project["event_id"]):
        missing["track_id"] = "Choose a track."
    answered = {r["question_id"] for r in fetch_all(
        db, "SELECT question_id FROM project_answers WHERE project_id = ? AND answer <> ''", (project["id"],))}
    for q in list_questions(db, project["event_id"]):
        if q["required"] and q["id"] not in answered:
            missing[f"q_{q['id']}"] = "This question is required."
    return missing


# ----------------------------------------------------------------- writes

def _write_tags(db: sqlite3.Connection, project_id: str, tags: list[str]) -> None:
    db.execute("DELETE FROM project_tags WHERE project_id = ?", (project_id,))
    db.executemany("INSERT INTO project_tags VALUES (?, ?)", [(project_id, t) for t in tags])


def _write_answers(db: sqlite3.Connection, project_id: str, answers: dict[str, str]) -> None:
    for question_id, answer in answers.items():
        db.execute(
            "INSERT INTO project_answers (project_id, question_id, answer) VALUES (?, ?, ?)"
            " ON CONFLICT (project_id, question_id) DO UPDATE SET answer = excluded.answer",
            (project_id, question_id, answer),
        )


def create_project(db: sqlite3.Connection, actor: User, event: Event, payload) -> str:
    title = payload.text("title")
    assert_submissions_open(db, event, actor=actor, title=title)
    team = team_for_user(db, actor.id, event.id)
    if team is None:
        raise Forbidden("Join or form a team for this event first.", code="no_team")
    if project_for_team(db, team["id"]):
        raise Conflict("Your team already has a project for this event. Edit that one instead.", code="project_exists")
    values, tags, answers = clean_submission(db, event, payload)
    if not values.get("title"):
        raise ValidationFailed(fields={"title": "Give the project a name."})
    project_id = new_id("prj")
    now = clock.now_iso()
    with transaction(db):
        db.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, tagline, description, video_url, repo_url,"
            " demo_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (project_id, event.id, team["id"], values.get("track_id") or None, values["title"],
             values.get("tagline", ""), values.get("description", ""), values.get("video_url", ""),
             values.get("repo_url", ""), values.get("demo_url", ""), now, now),
        )
        if tags:
            _write_tags(db, project_id, tags)
        _write_answers(db, project_id, answers)
        audit.record(db, "project.create", actor=actor, event_id=event.id, target_type="project",
                     target_id=project_id, detail={"title": values["title"]})
    return project_id


def update_project(db: sqlite3.Connection, actor: User, project_id: str, payload) -> list[str]:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    event = get_event(db, project["event_id"])
    assert_submissions_open(db, event, actor=actor, title=project["title"], project_id=project_id)
    values, tags, answers = clean_submission(db, event, payload)
    changed = [k for k, v in values.items() if (project[k] or "") != (v or "")]
    with transaction(db):
        if changed:
            sets = ", ".join(f"{k} = ?" for k in changed)
            new_values = [(values[k] or None) if k == "track_id" else values[k] for k in changed]
            db.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
                       (*new_values, clock.now_iso(), project_id))
        if tags is not None and tags != tags_for(db, project_id):
            _write_tags(db, project_id, tags)
            changed.append("tags")
        if answers:
            _write_answers(db, project_id, answers)
            changed.append("answers")
        if changed:
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (clock.now_iso(), project_id))
            audit.record(db, "project.update", actor=actor, event_id=event.id, target_type="project",
                         target_id=project_id, detail={"title": values.get("title", project["title"]), "fields": changed})
            if project["status"] == "submitted":
                webhooks.emit(db, event.id, "project.updated", {"project_id": project_id, "fields": changed})
    return changed


def submit_project(db: sqlite3.Connection, actor: User, project_id: str) -> None:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    event = get_event(db, project["event_id"])
    assert_submissions_open(db, event, actor=actor, title=project["title"], project_id=project_id)
    missing = missing_for_submission(db, project)
    if missing:
        raise ValidationFailed("A few things are missing before this can be submitted.", fields=missing)
    now = clock.now_iso()
    with transaction(db):
        db.execute("UPDATE projects SET status = 'submitted', submitted_at = COALESCE(submitted_at, ?), updated_at = ?"
                   " WHERE id = ?", (now, now, project_id))
        audit.record(db, "project.submit", actor=actor, event_id=event.id, target_type="project",
                     target_id=project_id, detail={"title": project["title"]})
        duplicates.refresh(db, event.id)
        webhooks.emit(db, event.id, "project.submitted", {
            "project_id": project_id, "title": project["title"], "team_id": project["team_id"],
            "track_id": project["track_id"], "url": f"/projects/{project_id}"})


def unsubmit_project(db: sqlite3.Connection, actor: User, project_id: str) -> None:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    event = get_event(db, project["event_id"])
    assert_submissions_open(db, event, actor=actor, title=project["title"], project_id=project_id)
    with transaction(db):
        db.execute("UPDATE projects SET status = 'draft', updated_at = ? WHERE id = ?", (clock.now_iso(), project_id))
        audit.record(db, "project.withdraw", actor=actor, event_id=event.id, target_type="project",
                     target_id=project_id, detail={"title": project["title"]})


def set_thumbnail(db: sqlite3.Connection, settings: Settings, actor: User, project_id: str, upload: UploadFile) -> None:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    assert_submissions_open(db, get_event(db, project["event_id"]), actor=actor, title=project["title"], project_id=project_id)
    name = uploads.save_image(settings, upload, field="thumbnail")
    db.execute("UPDATE projects SET thumbnail = ?, updated_at = ? WHERE id = ?", (name, clock.now_iso(), project_id))
    uploads.delete_image(settings, project["thumbnail"])


def add_image(db: sqlite3.Connection, settings: Settings, actor: User, project_id: str,
              upload: UploadFile, caption: str = "") -> str:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    assert_submissions_open(db, get_event(db, project["event_id"]), actor=actor, title=project["title"], project_id=project_id)
    count = fetch_value(db, "SELECT COUNT(*) FROM project_images WHERE project_id = ?", (project_id,))
    if count >= MAX_IMAGES:
        raise ValidationFailed(fields={"image": f"Galleries hold up to {MAX_IMAGES} images."})
    name = uploads.save_image(settings, upload)
    image_id = new_id("img")
    db.execute("INSERT INTO project_images (id, project_id, file, caption, position) VALUES (?, ?, ?, ?, ?)",
               (image_id, project_id, name, caption.strip()[:140], count))
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (clock.now_iso(), project_id))
    return image_id


def remove_image(db: sqlite3.Connection, settings: Settings, actor: User, project_id: str, image_id: str) -> None:
    project = get_project(db, project_id)
    require_editor(db, actor, project)
    assert_submissions_open(db, get_event(db, project["event_id"]), actor=actor, title=project["title"], project_id=project_id)
    image = fetch_one(db, "SELECT * FROM project_images WHERE id = ? AND project_id = ?", (image_id, project_id))
    if image is None:
        raise NotFound("No such image.")
    db.execute("DELETE FROM project_images WHERE id = ?", (image_id,))
    uploads.delete_image(settings, image["file"])


def latest_participant_event(db: sqlite3.Connection, user: User) -> Event | None:
    """The event a bare POST /projects/new most plausibly targets."""
    row = fetch_one(
        db,
        "SELECT e.id FROM team_members m JOIN events e ON e.id = m.event_id WHERE m.user_id = ?"
        " ORDER BY e.submissions_close_at DESC LIMIT 1",
        (user.id,),
    )
    return get_event(db, row["id"]) if row else None


def serialize(project: sqlite3.Row | dict, tags: list[str] | None = None) -> dict[str, Any]:
    data = {k: project[k] for k in ("id", "event_id", "team_id", "track_id", "title", "tagline", "description",
                                     "video_url", "repo_url", "demo_url", "status", "submitted_at", "updated_at")}
    data["thumbnail_url"] = f"/uploads/{project['thumbnail']}" if project["thumbnail"] else None
    if tags is not None:
        data["tags"] = tags
    return data
