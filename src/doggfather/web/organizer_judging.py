"""Organizer console, judging setup: rubric, judges, assignments."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..auth import CurrentUser, RequiredUser
from ..deps import DB, AppSettings, Form
from ..errors import AppError, ValidationFailed
from ..services import assignment, exports, judges, normalization, progress, results, rubric
from ..services import events as event_service
from .organizer import organizer_event
from .templating import redirect, render

router = APIRouter(prefix="/organize", include_in_schema=False)
invites = APIRouter(include_in_schema=False)


# ------------------------------------------------------------------ rubric

def _rubric_page(request: Request, db, event, *, errors=None, status_code=200):
    criteria = rubric.list_criteria(db, event.id)
    total_weight = sum(c["weight"] for c in criteria) or 1
    return render(request, "organize/rubric.html", {
        "event": event, "tab": "rubric", "errors": errors or {},
        "criteria": criteria, "total_weight": total_weight,
        "tracks": event_service.list_tracks(db, event.id),
        "overrides": rubric.track_overrides(db, event.id),
        "locked": rubric.scoring_started(db, event.id),
    }, status_code=status_code)


@router.get("/{slug}/rubric")
def rubric_page(request: Request, db: DB, user: RequiredUser, slug: str):
    return _rubric_page(request, db, organizer_event(db, user, slug))


@router.post("/{slug}/rubric")
def rubric_submit(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    rows, overrides = [], {}
    try:
        count = min(int(str(form.get("rows") or "0")), 100)
    except ValueError:
        return redirect(request, f"/organize/{slug}/rubric", "That form was malformed; reload and try again.", "error")
    for i in range(count):
        rows.append({
            "id": str(form.get(f"c{i}_id") or "") or None,
            "label": form.get(f"c{i}_label"),
            "description": form.get(f"c{i}_description"),
            "weight": form.get(f"c{i}_weight"),
            "min_score": form.get(f"c{i}_min"),
            "max_score": form.get(f"c{i}_max"),
            "delete": form.get(f"c{i}_delete") == "on",
        })
    for key in form.keys():
        if key.startswith("ov__"):
            _, criterion_id, track_id = key.split("__", 2)
            overrides[(criterion_id, track_id)] = form.get(key)
    try:
        changes = rubric.update_rubric(db, user, event, rows, overrides)
    except ValidationFailed as exc:
        return _rubric_page(request, db, event, errors=exc.fields, status_code=422)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/rubric", exc.message, "error")
    return redirect(request, f"/organize/{slug}/rubric", "Rubric saved." if changes else "No changes.")


# ------------------------------------------------------------------ judges

def _judges_page(request: Request, db, event, *, errors=None, status_code=200):
    return render(request, "organize/judges.html", {
        "event": event, "tab": "judges", "errors": errors or {},
        "roster": judges.roster(db, event.id),
        "invites": judges.pending_invites(db, event.id),
        "tracks": event_service.list_tracks(db, event.id),
    }, status_code=status_code)


@router.get("/{slug}/judges")
def judges_page(request: Request, db: DB, user: RequiredUser, slug: str):
    return _judges_page(request, db, organizer_event(db, user, slug))


@router.post("/{slug}/judges/invite")
def invite(request: Request, db: DB, settings: AppSettings, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        judges.invite_judge(db, settings, user, event, str(form.get("email") or ""),
                            [str(t) for t in form.getlist("tracks")], str(form.get("note") or ""))
    except ValidationFailed as exc:
        return _judges_page(request, db, event, errors=exc.fields, status_code=422)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/judges", exc.message, "error")
    return redirect(request, f"/organize/{slug}/judges", "Invitation sent (see the outbox).")


@router.post("/{slug}/judges/{judge_id}/tracks")
def judge_tracks(request: Request, db: DB, user: RequiredUser, slug: str, judge_id: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        judges.set_tracks(db, user, event, judge_id, [str(t) for t in form.getlist("tracks")])
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/judges", exc.message, "error")
    return redirect(request, f"/organize/{slug}/judges", "Tracks updated.")


@router.post("/{slug}/judges/{judge_id}/remove")
def remove(request: Request, db: DB, user: RequiredUser, slug: str, judge_id: str):
    event = organizer_event(db, user, slug)
    try:
        judges.remove_judge(db, user, event, judge_id)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/judges", exc.message, "error")
    return redirect(request, f"/organize/{slug}/judges", "Judge removed.")


# ------------------------------------------------------------- assignments

@router.get("/{slug}/assignments")
def assignments_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    rows = assignment.matrix(db, event.id)
    return render(request, "organize/assignments.html", {
        "event": event, "tab": "assignments", "rows": rows,
        "roster": judges.roster(db, event.id),
        "short": sum(1 for r in rows if len(r["reviews"]) < event.review_target),
        "preview": assignment.plan_auto(db, event),
    })


@router.post("/{slug}/assignments/auto")
def auto(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        target = int(str(form.get("target") or event.review_target))
        plan = assignment.run_auto(db, user, event, target)
    except ValueError:
        return redirect(request, f"/organize/{slug}/assignments", "Reviews per project must be a whole number.", "error")
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/assignments", exc.message, "error")
    note = f" {len(plan.shortfall)} projects lack eligible judges." if plan.shortfall else ""
    return redirect(request, f"/organize/{slug}/assignments",
                    f"Batch {plan.batch}: {len(plan.created)} new assignments.{note}")


@router.post("/{slug}/assignments/batch")
def batch(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        label = assignment.assign_batch(db, user, event, str(form.get("judge") or ""),
                                        [str(p) for p in form.getlist("projects")])
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/assignments", exc.message, "error")
    return redirect(request, f"/organize/{slug}/assignments", f"Batch {label} assigned.")


@router.post("/{slug}/assignments/{assignment_id}/delete")
def delete(request: Request, db: DB, user: RequiredUser, slug: str, assignment_id: str):
    event = organizer_event(db, user, slug)
    try:
        assignment.unassign(db, user, event, assignment_id)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/assignments", exc.message, "error")
    return redirect(request, f"/organize/{slug}/assignments", "Assignment removed.")


# -------------------------------------------------------- invite acceptance

@invites.get("/invite/{token}")
def invite_page(request: Request, db: DB, user: CurrentUser, token: str):
    invite = judges.invite_for_token(db, token)
    event = event_service.get_event(db, invite["event_id"])
    return render(request, "judge/invite.html", {"invite": invite, "event": event, "token": token})


@invites.post("/invite/{token}")
def invite_accept(request: Request, db: DB, user: RequiredUser, token: str):
    try:
        event = judges.accept_invite(db, user, token)
    except AppError as exc:
        return redirect(request, f"/invite/{token}", exc.message, "error")
    return redirect(request, f"/judge/{event.slug}", f"You are a judge for {event.name}.")


# ----------------------------------------------------------------- results

@router.get("/{slug}/results")
def results_page(request: Request, db: DB, user: RequiredUser, slug: str, method: str = ""):
    event = organizer_event(db, user, slug)
    table = results.organizer_table(db, user, event, method or None)
    return render(request, "organize/results.html", {
        "event": event, "tab": "results", "table": table,
        "methods": normalization.METHOD_LABELS,
    })


@router.post("/{slug}/results")
def results_action(request: Request, db: DB, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    action = str(form.get("action") or "")
    try:
        if action == "method":
            results.set_method(db, user, event, str(form.get("method") or ""))
            message = "Ranking method saved."
        elif action == "publish":
            results.publish(db, user, event)
            message = "Results published. They are now public."
        elif action == "unpublish":
            results.unpublish(db, user, event)
            message = "Results hidden again."
        else:
            raise ValidationFailed("Unknown action.")
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/results", exc.message, "error")
    return redirect(request, f"/organize/{slug}/results", message)


# ---------------------------------------------------------------- progress

@router.get("/{slug}/progress")
def progress_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    return render(request, "organize/progress.html", {
        "event": event, "tab": "progress", "snap": progress.snapshot(db, event),
    })


# ----------------------------------------------------------------- exports

@router.get("/{slug}/exports")
def exports_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    return render(request, "organize/exports.html", {"event": event, "tab": "exports", "kinds": exports.KINDS})
