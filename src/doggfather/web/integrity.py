"""Anti-abuse and accountability: comments, duplicate flags, moderation,
refused late writes, and the per-event audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import audit
from ..auth import RequiredUser
from ..db import fetch_value
from ..deps import DB, AppSettings, Form, Limiter, client_ip
from ..errors import AppError, ValidationFailed
from ..security import keyed_hash
from ..services import comments, duplicates
from .organizer import organizer_event
from .templating import redirect, render

router = APIRouter(include_in_schema=False)


# --------------------------------------------------------------- comments

@router.post("/projects/{project_id}/comments")
def post_comment(request: Request, db: DB, settings: AppSettings, user: RequiredUser, limiter: Limiter,
                 project_id: str, form: Form):
    limiter.enforce(f"comment:user:{user.id}", 5, 60, "You are commenting very fast. Take a breath.")
    limiter.enforce(f"comment:user-day:{user.id}", 50, 86400)
    try:
        comments.add_comment(db, user, project_id, str(form.get("body") or ""),
                             keyed_hash(settings.secret_key, client_ip(request), "comment-ip"))
    except ValidationFailed as exc:
        return redirect(request, f"/projects/{project_id}#comments", next(iter(exc.fields.values())), "error")
    except AppError as exc:
        return redirect(request, f"/projects/{project_id}#comments", exc.message, "error")
    return redirect(request, f"/projects/{project_id}#comments", "Comment posted.")


@router.post("/comments/{comment_id}")
def moderate_comment(request: Request, db: DB, user: RequiredUser, comment_id: str, form: Form):
    row = comments.set_hidden(db, user, comment_id, form.get("action") == "hide", str(form.get("reason") or ""))
    back = str(form.get("back") or f"/projects/{row['project_id']}#comments")
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return redirect(request, back, "Comment hidden." if form.get("action") == "hide" else "Comment restored.")


# -------------------------------------------------------------- integrity

@router.get("/organize/{slug}/integrity")
def integrity_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    return render(request, "organize/integrity.html", {
        "event": event, "tab": "integrity",
        "duplicates": duplicates.flagged(db, event.id),
        "comments": comments.recent_for_event(db, event.id),
        "refused": audit.entries(db, event_id=event.id, action_prefix="project.refused", limit=50),
        "flagged_voters": fetch_value(db, "SELECT COUNT(*) FROM voters WHERE event_id = ? AND flagged IS NOT NULL"
                                          " AND voided_at IS NULL", (event.id,), default=0),
    })


@router.post("/organize/{slug}/duplicates/scan")
def scan(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    found = duplicates.refresh(db, event.id, user)
    return redirect(request, f"/organize/{slug}/integrity", f"Scan complete: {len(found)} possible duplicates.")


@router.post("/organize/{slug}/duplicates/{project_id}/dismiss")
def dismiss(request: Request, db: DB, user: RequiredUser, slug: str, project_id: str):
    duplicates.dismiss(db, user, organizer_event(db, user, slug), project_id)
    return redirect(request, f"/organize/{slug}/integrity", "Flag dismissed; it will not come back.")


@router.post("/organize/{slug}/projects/{project_id}/withdraw")
def withdraw(request: Request, db: DB, user: RequiredUser, slug: str, project_id: str, form: Form):
    duplicates.withdraw(db, user, organizer_event(db, user, slug), project_id, str(form.get("reason") or ""))
    return redirect(request, f"/organize/{slug}/integrity", "Project withdrawn from the gallery and the results.")


# ------------------------------------------------------------------ audit

@router.get("/organize/{slug}/audit")
def audit_page(request: Request, db: DB, user: RequiredUser, slug: str, kind: str = ""):
    event = organizer_event(db, user, slug)
    return render(request, "organize/audit.html", {
        "event": event, "tab": "audit", "kind": kind,
        "entries": audit.entries(db, event_id=event.id, limit=500, action_prefix=kind or None),
        "chain": audit.verify_chain(db),
        "kinds": ["project", "team", "score", "assign", "judge", "rubric", "results", "vote", "comment", "event", "export"],
    })
