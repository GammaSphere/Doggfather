"""Public event pages."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import policy
from ..auth import CurrentUser
from ..db import fetch_all
from ..deps import DB
from ..errors import ResultsHidden
from ..services import events as event_service
from ..services import normalization, results
from .templating import render

router = APIRouter(include_in_schema=False)


@router.get("/events")
def events_index(request: Request, db: DB):
    return render(request, "events/list.html", {"items": event_service.list_events(db)})


@router.get("/events/{slug}")
def event_page(request: Request, db: DB, user: CurrentUser, slug: str):
    event = event_service.get_event_by_slug(db, slug)
    preview = fetch_all(
        db,
        "SELECT p.id, p.title, p.tagline, p.thumbnail, t.name AS track_name, tm.name AS team_name"
        " FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id"
        " WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY p.submitted_at DESC LIMIT 6",
        (event.id,),
    )
    return render(request, "events/detail.html", {
        "event": event,
        "tracks": event_service.list_tracks(db, event.id),
        "prizes": event_service.list_prizes(db, event.id),
        "counts": event_service.event_counts(db, event.id),
        "roles": policy.roles_in_event(db, user, event.id),
        "preview": preview,
    })


@router.get("/events/{slug}/results")
def results_page(request: Request, db: DB, user: CurrentUser, slug: str):
    event = event_service.get_event_by_slug(db, slug)
    try:
        table = results.visible_table(db, user, event)
    except ResultsHidden as exc:
        return render(request, "events/sealed.html", {"event": event, "message": exc.message}, status_code=403)
    leaders: dict[str, dict] = {}
    for row in table.rows:
        if row["track_name"] and row["track_name"] not in leaders:
            leaders[row["track_name"]] = row
    return render(request, "events/results.html", {
        "event": event, "table": table, "leaders": leaders,
        "method_label": normalization.METHOD_LABELS[table.method],
        "preview": not event.results_published,
    })


@router.get("/judging")
def judging_method(request: Request):
    return render(request, "judging.html", {})
