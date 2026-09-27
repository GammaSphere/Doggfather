"""Organizer console: create and run events.

Every handler resolves the event and calls ``policy.require_organizer``
before doing anything else; the services check again, so a route added
without the guard still cannot write.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request

from .. import clock, policy
from ..auth import RequiredUser, User
from ..deps import DB, Form
from ..errors import AppError, Forbidden, ValidationFailed
from ..services import bundles
from ..services import events as event_service
from ..services.events import Event
from .templating import redirect, render

router = APIRouter(prefix="/organize", include_in_schema=False)


def organizer_event(db, user: User, slug: str) -> Event:
    event = event_service.get_event_by_slug(db, slug)
    policy.require_organizer(db, user, event.id)
    return event


def _form_dict(form) -> dict[str, Any]:
    return {key: form.get(key) for key in form.keys()}


# ------------------------------------------------------------------ index

@router.get("")
def index(request: Request, db: DB, user: RequiredUser):
    ids = policy.organizer_event_ids(db, user)
    items = [item for item in event_service.list_events(db) if item["event"].id in ids]
    return render(request, "organize/index.html", {
        "items": items,
        "can_create": event_service.can_create_events(db, user),
    })


def _defaults() -> dict[str, str]:
    start = clock.now().replace(minute=0, second=0) + timedelta(hours=1)
    return {
        "submissions_open_at": clock.iso(start),
        "submissions_close_at": clock.iso(start + timedelta(hours=72)),
        "judging_close_at": clock.iso(start + timedelta(days=10)),
        "max_team_size": "4",
        "review_target": "3",
    }


@router.get("/new")
def new_event_page(request: Request, db: DB, user: RequiredUser):
    if not event_service.can_create_events(db, user):
        raise Forbidden("Ask an admin to make you an organizer first.")
    return render(request, "organize/new.html", {"values": _defaults(), "errors": {},
                                                 "voting_modes": event_service.VOTING_MODES})


@router.post("/new")
def new_event_submit(request: Request, db: DB, user: RequiredUser, form: Form):
    if not event_service.can_create_events(db, user):
        raise Forbidden("Ask an admin to make you an organizer first.")
    values = _form_dict(form)
    try:
        event = event_service.create_event(db, user, values)
    except ValidationFailed as exc:
        return render(request, "organize/new.html", {"values": values, "errors": exc.fields,
                                                     "voting_modes": event_service.VOTING_MODES}, status_code=422)
    return redirect(request, f"/organize/{event.slug}", f"“{event.name}” is live. Add tracks and prizes next.")


@router.post("/import")
def import_event(request: Request, db: DB, user: RequiredUser, form: Form):
    if not event_service.can_create_events(db, user):
        raise Forbidden("Ask an admin to make you an organizer first.")
    upload = form.get("bundle")
    try:
        data = json.loads(upload.file.read(20 * 1024 * 1024)) if hasattr(upload, "file") else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return redirect(request, "/organize", "That file is not a JSON bundle.", "error")
    try:
        report = bundles.import_bundle(db, data, actor=user)
    except AppError as exc:
        return redirect(request, "/organize", exc.message, "error")
    event = event_service.get_event(db, report.event_id)
    note = f" ({len(report.warnings)} warnings)" if report.warnings else ""
    return redirect(request, f"/organize/{event.slug}", f"Imported: {report.summary()}{note}.")


# --------------------------------------------------------------- overview

@router.get("/{slug}")
def overview(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    return render(request, "organize/overview.html", {
        "event": event,
        "tab": "overview",
        "counts": event_service.event_counts(db, event.id),
        "tracks": event_service.list_tracks(db, event.id),
        "phase_actions": event_service.PHASE_ACTIONS,
    })


@router.post("/{slug}/phase")
def phase(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    action = str(form.get("action") or "")
    try:
        event_service.apply_phase_action(db, user, event, action)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}", exc.message, "error")
    return redirect(request, f"/organize/{slug}", event_service.PHASE_ACTIONS.get(action, "Updated") + ".")


# --------------------------------------------------------------- settings

def _settings_page(request: Request, db, event: Event, *, values=None, errors=None, status_code=200):
    return render(request, "organize/settings.html", {
        "event": event,
        "tab": "settings",
        "values": values or {k: getattr(event, k) for k in event_service.EDITABLE},
        "errors": errors or {},
        "tracks": event_service.list_tracks(db, event.id),
        "prizes": event_service.list_prizes(db, event.id),
        "questions": event_service.list_questions(db, event.id),
        "organizers": event_service.organizers(db, event.id),
        "question_kinds": event_service.QUESTION_KINDS,
        "voting_modes": event_service.VOTING_MODES,
    }, status_code=status_code)


@router.get("/{slug}/settings")
def settings_page(request: Request, db: DB, user: RequiredUser, slug: str):
    return _settings_page(request, db, organizer_event(db, user, slug))


@router.post("/{slug}/settings")
def settings_submit(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    values = _form_dict(form)
    try:
        event = event_service.update_event(db, user, event, values)
    except ValidationFailed as exc:
        return _settings_page(request, db, event, values=values, errors=exc.fields, status_code=422)
    return redirect(request, f"/organize/{event.slug}/settings", "Event settings saved.")


def _sub_action(request: Request, db, user: User, slug: str, fn, success: str) -> Any:
    event = organizer_event(db, user, slug)
    try:
        fn(event)
    except ValidationFailed as exc:
        return _settings_page(request, db, event, errors=exc.fields, status_code=422)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/settings", exc.message, "error")
    return redirect(request, f"/organize/{slug}/settings", success)


@router.post("/{slug}/tracks")
def add_track(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    return _sub_action(request, db, user, slug, lambda e: event_service.add_track(
        db, user, e, str(form.get("track_name") or ""), str(form.get("track_description") or "")), "Track added.")


@router.post("/{slug}/tracks/{track_id}/delete")
def remove_track(request: Request, db: DB, user: RequiredUser, slug: str, track_id: str):
    return _sub_action(request, db, user, slug, lambda e: event_service.remove_track(db, user, e, track_id),
                       "Track removed.")


@router.post("/{slug}/prizes")
def add_prize(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    return _sub_action(request, db, user, slug, lambda e: event_service.add_prize(
        db, user, e, str(form.get("prize_name") or ""), str(form.get("prize_value") or ""),
        str(form.get("prize_description") or ""), str(form.get("prize_track") or "") or None), "Prize added.")


@router.post("/{slug}/prizes/{prize_id}/delete")
def remove_prize(request: Request, db: DB, user: RequiredUser, slug: str, prize_id: str):
    return _sub_action(request, db, user, slug, lambda e: event_service.remove_prize(db, user, e, prize_id),
                       "Prize removed.")


@router.post("/{slug}/questions")
def add_question(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    options = str(form.get("question_options") or "").replace("\r", "").split("\n")
    return _sub_action(request, db, user, slug, lambda e: event_service.add_question(
        db, user, e, str(form.get("question_prompt") or ""), str(form.get("question_kind") or "text"),
        form.get("question_required") == "on", options, str(form.get("question_help") or "")), "Question added.")


@router.post("/{slug}/questions/{question_id}/delete")
def remove_question(request: Request, db: DB, user: RequiredUser, slug: str, question_id: str):
    return _sub_action(request, db, user, slug, lambda e: event_service.remove_question(db, user, e, question_id),
                       "Question removed.")


@router.post("/{slug}/organizers")
def add_organizer(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    return _sub_action(request, db, user, slug, lambda e: event_service.add_organizer(
        db, user, e, str(form.get("organizer_email") or "")), "Organizer added.")
