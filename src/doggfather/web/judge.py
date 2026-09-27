"""Judge pages: event list, scoring queue, and the scoring screen.

Built to cut review fatigue: the project and the rubric sit on one screen,
every criterion takes a single keypress, and saving goes straight to the
next project in the queue.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import policy
from ..auth import RequiredUser
from ..deps import DB, Form
from ..errors import ValidationFailed
from ..services import events as event_service
from ..services import projects as project_service
from ..services import rubric, scoring
from .templating import redirect, render

router = APIRouter(prefix="/judge", include_in_schema=False)

# A rough benchmark: incumbents report ~10 minutes per project; we show a
# live estimate from the judge's own pace instead where we have one.
DEFAULT_MINUTES_PER_PROJECT = 6


@router.get("")
def index(request: Request, db: DB, user: RequiredUser):
    events = scoring.judge_events(db, user.id)
    if len(events) == 1:
        return RedirectResponse(f"/judge/{events[0]['slug']}", status_code=303)
    return render(request, "judge/index.html", {"events": events})


@router.get("/{slug}")
def queue_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = event_service.get_event_by_slug(db, slug)
    policy.require_judge(db, user, event.id)
    items = scoring.queue(db, user.id, event.id)
    done = sum(1 for i in items if i["status"] == "done")
    return render(request, "judge/queue.html", {
        "event": event,
        "items": items,
        "done": done,
        "remaining": len(items) - done,
        "minutes_left": (len(items) - done) * DEFAULT_MINUTES_PER_PROJECT,
        "next_id": scoring.next_pending(db, user.id, event.id),
        "tracks": [t["name"] for t in event_service.list_tracks(db, event.id)
                   if t["id"] in policy.judge_track_ids(db, user.id, event.id)],
    })


def _score_page(request: Request, db, user, event, project, *, values=None, comment=None, errors=None, status_code=200):
    items = scoring.queue(db, user.id, event.id)
    position = next((i for i, q in enumerate(items) if q["id"] == project["id"]), 0)
    existing = scoring.own_score(db, user.id, project["id"])
    criteria = rubric.list_criteria(db, event.id)
    weights = rubric.weights_for(rubric.weight_table(db, event.id), project["track_id"])
    if values is None:
        values = {c["id"]: (existing or {}).get("items", {}).get(c["id"]) for c in criteria}
    return render(request, "judge/score.html", {
        "event": event,
        "p": project_service.project_detail(db, project["id"]),
        "criteria": criteria,
        "weights": weights,
        "total_weight": sum(weights.get(c["id"], 0) for c in criteria) or 1,
        "values": values,
        "comment": comment if comment is not None else (existing or {}).get("comment", ""),
        "existing": existing,
        "errors": errors or {},
        "position": position + 1,
        "count": len(items),
        "done": sum(1 for i in items if i["status"] == "done"),
        "open": event.judging_open(),
    }, status_code=status_code)


@router.get("/{slug}/projects/{project_id}")
def score_page(request: Request, db: DB, user: RequiredUser, slug: str, project_id: str):
    event = event_service.get_event_by_slug(db, slug)
    project = scoring.project_for_judge(db, user, project_id)
    if project["event_id"] != event.id:
        return RedirectResponse(f"/judge/{slug}", status_code=303)
    return _score_page(request, db, user, event, project)


@router.post("/{slug}/projects/{project_id}")
def score_submit(request: Request, db: DB, user: RequiredUser, slug: str, project_id: str, form: Form):
    event = event_service.get_event_by_slug(db, slug)
    project = scoring.project_for_judge(db, user, project_id)
    criteria = rubric.list_criteria(db, event.id)
    values = {c["id"]: form.get(f"c_{c['id']}") for c in criteria}
    comment = str(form.get("comment") or "")
    try:
        scoring.save_score(db, user, project_id, values, comment)
    except ValidationFailed as exc:
        return _score_page(request, db, user, event, project, values=values, comment=comment, errors=exc.fields,
                           status_code=422)
    next_id = scoring.next_pending(db, user.id, event.id, after=project_id)
    if next_id and form.get("then") != "stay":
        return redirect(request, f"/judge/{slug}/projects/{next_id}", f"Saved “{project['title']}”. Next one up.")
    if next_id:
        return redirect(request, f"/judge/{slug}/projects/{project_id}", "Saved.")
    return redirect(request, f"/judge/{slug}", "Saved. That was your last pending project. Thank you!")
