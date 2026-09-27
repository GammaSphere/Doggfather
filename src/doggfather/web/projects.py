"""Submission pages: create, edit, submit; project detail; uploaded images.

``POST /projects/new`` serves browsers (form posts) and API clients (JSON)
from one handler, since it is also the route the acceptance checker probes
after the deadline. Both paths go through the same service call, so both
are refused the same way once submissions close.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import policy
from ..auth import CurrentUser, RequiredUser, User
from ..deps import DB, AppSettings, Body, Limiter
from ..errors import Forbidden, NotAuthenticated, NotFound, ValidationFailed
from ..services import events as event_service
from ..services import projects as project_service
from ..services import comments as comment_service
from ..services import uploads
from ..services.teams import team_for_user
from .templating import redirect, render

router = APIRouter(include_in_schema=False)


def _resolve_event(db, user: User, body) -> event_service.Event:
    for key in ("event_id", "event"):
        value = body.text(key)
        if value:
            try:
                return event_service.get_event(db, value)
            except NotFound:
                return event_service.get_event_by_slug(db, value)
    event = project_service.latest_participant_event(db, user)
    if event is None:
        raise Forbidden("Join or form a team for an event before submitting.", code="no_team")
    return event


def can_view(db, user: User | None, project) -> bool:
    if project["status"] == "submitted":
        return True
    return project_service.is_team_member(db, user, project) or policy.is_organizer(db, user, project["event_id"])


def _form_page(request: Request, db, event, *, project=None, values=None, errors=None, status_code=200):
    detail = project_service.project_detail(db, project["id"]) if project else None
    if values is None:
        values = dict(detail) if detail else {}
        if detail:
            values["tags"] = " ".join(detail["tags"])
            for answer in detail["answers"]:
                values[f"q_{answer['id']}"] = answer["answer"] or ""
    missing = project_service.missing_for_submission(db, project) if project else {}
    return render(request, "projects/form.html", {
        "event": event,
        "project": detail,
        "values": values,
        "errors": errors or {},
        "tracks": event_service.list_tracks(db, event.id),
        "questions": event_service.list_questions(db, event.id),
        "missing": missing,
        "is_open": event.submissions_open(),
    }, status_code=status_code)


# ------------------------------------------------------------------ create

@router.get("/projects/new")
def new_project_page(request: Request, db: DB, user: RequiredUser, event: str = ""):
    ev = event_service.get_event_by_slug(db, event) if event else project_service.latest_participant_event(db, user)
    if ev is None:
        return redirect(request, "/events", "Pick an event and form a team first.", "info")
    team = team_for_user(db, user.id, ev.id)
    if team is None:
        return redirect(request, f"/events/{ev.slug}/team", "Form or join a team before starting a submission.", "info")
    existing = project_service.project_for_team(db, team["id"])
    if existing:
        return redirect(request, f"/projects/{existing['id']}/edit")
    project_service.assert_submissions_open(db, ev, actor=user)
    return _form_page(request, db, ev)


@router.post("/projects/new")
def new_project_submit(request: Request, db: DB, user: CurrentUser, limiter: Limiter, body: Body):
    if user is None:
        raise NotAuthenticated()
    limiter.enforce(f"project-write:{user.id}", 60, 60)
    event = _resolve_event(db, user, body)
    try:
        project_id = project_service.create_project(db, user, event, body)
    except ValidationFailed as exc:
        if body.is_json:
            raise
        values = {k: body.get(k) for k in body.keys()}
        return _form_page(request, db, event, values=values, errors=exc.fields, status_code=422)
    if body.is_json:
        project = project_service.get_project(db, project_id)
        return JSONResponse(project_service.serialize(project, project_service.tags_for(db, project_id)),
                            status_code=201, headers={"Location": f"/projects/{project_id}"})
    return redirect(request, f"/projects/{project_id}/edit", "Draft saved. Keep editing until the deadline.")


# -------------------------------------------------------------------- view

@router.get("/projects/{project_id}")
def project_page(request: Request, db: DB, user: CurrentUser, project_id: str):
    project = project_service.get_project(db, project_id)
    if not can_view(db, user, project):
        raise NotFound("No such project.")
    event = event_service.get_event(db, project["event_id"])
    is_organizer = policy.is_organizer(db, user, event.id)
    return render(request, "projects/detail.html", {
        "event": event,
        "p": project_service.project_detail(db, project_id),
        "is_editor": project_service.is_team_member(db, user, project),
        "is_organizer": is_organizer,
        "comments": comment_service.list_comments(db, project_id, include_hidden=is_organizer),
    })


# -------------------------------------------------------------------- edit

@router.get("/projects/{project_id}/edit")
def edit_page(request: Request, db: DB, user: RequiredUser, project_id: str):
    project = project_service.get_project(db, project_id)
    project_service.require_editor(db, user, project)
    return _form_page(request, db, event_service.get_event(db, project["event_id"]), project=project)


@router.post("/projects/{project_id}/edit")
def edit_submit(request: Request, db: DB, settings: AppSettings, user: RequiredUser, limiter: Limiter,
                project_id: str, body: Body):
    limiter.enforce(f"project-write:{user.id}", 60, 60)
    project = project_service.get_project(db, project_id)
    event = event_service.get_event(db, project["event_id"])
    try:
        project_service.update_project(db, user, project_id, body)
        thumbnail = body.file("thumbnail")
        if thumbnail:
            project_service.set_thumbnail(db, settings, user, project_id, thumbnail)
        if body.text("action") == "submit":
            project_service.submit_project(db, user, project_id)
    except ValidationFailed as exc:
        if body.is_json:
            raise
        values = {k: body.get(k) for k in body.keys() if k != "thumbnail"}
        return _form_page(request, db, event, project=project_service.get_project(db, project_id), values=values,
                          errors=exc.fields, status_code=422)
    if body.is_json:
        return _project_json(db, project_id)
    message = "Submitted. You can keep editing until the deadline." if body.text("action") == "submit" else "Saved."
    return redirect(request, f"/projects/{project_id}/edit", message)


def _project_json(db, project_id: str) -> dict:
    return project_service.serialize(project_service.get_project(db, project_id), project_service.tags_for(db, project_id))


@router.post("/projects/{project_id}/submit")
def submit(request: Request, db: DB, user: RequiredUser, project_id: str, body: Body):
    try:
        project_service.submit_project(db, user, project_id)
    except ValidationFailed as exc:
        if body.is_json:
            raise
        missing = ", ".join(exc.fields)
        return redirect(request, f"/projects/{project_id}/edit", f"Not yet: fill in {missing}.", "error")
    if body.is_json:
        return _project_json(db, project_id)
    return redirect(request, f"/projects/{project_id}", "Submitted. It is now in the public gallery.")


@router.post("/projects/{project_id}/unsubmit")
def unsubmit(request: Request, db: DB, user: RequiredUser, project_id: str, body: Body):
    project_service.unsubmit_project(db, user, project_id)
    if body.is_json:
        return _project_json(db, project_id)
    return redirect(request, f"/projects/{project_id}/edit", "Back to draft. It is hidden from the gallery until you submit again.")


@router.post("/projects/{project_id}/images")
def add_image(request: Request, db: DB, settings: AppSettings, user: RequiredUser, project_id: str, body: Body):
    upload = body.file("image")
    if upload is None:
        return redirect(request, f"/projects/{project_id}/edit", "Choose an image to upload.", "error")
    try:
        project_service.add_image(db, settings, user, project_id, upload, body.text("caption"))
    except ValidationFailed as exc:
        return redirect(request, f"/projects/{project_id}/edit", next(iter(exc.fields.values())), "error")
    return redirect(request, f"/projects/{project_id}/edit", "Image added.")


@router.post("/projects/{project_id}/images/{image_id}/delete")
def remove_image(request: Request, db: DB, settings: AppSettings, user: RequiredUser, project_id: str, image_id: str):
    project_service.remove_image(db, settings, user, project_id, image_id)
    return redirect(request, f"/projects/{project_id}/edit", "Image removed.")


# ----------------------------------------------------------------- uploads

@router.get("/uploads/{name}")
def uploaded_file(settings: AppSettings, name: str):
    path = uploads.upload_path(settings, name)
    if path is None:
        raise NotFound("No such file.")
    return FileResponse(path, media_type=uploads.MEDIA_TYPES[path.suffix.lstrip(".")], headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })
