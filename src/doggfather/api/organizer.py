"""Organizer data endpoints: live progress and CSV exports."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from .. import policy
from ..auth import CurrentUser
from ..deps import DB
from ..services import events as event_service
from ..services import exports, progress

router = APIRouter(prefix="/api", tags=["organizer"])


@router.get("/events/{event_id}/progress", summary="Live judging progress (organizers)")
def event_progress(db: DB, user: CurrentUser, event_id: str):
    event = event_service.get_event(db, event_id)
    policy.require_organizer(db, user, event.id)
    return progress.snapshot(db, event)


@router.get("/events/{event_id}/export/{kind}.csv", summary="CSV export (organizers)",
            response_class=Response, responses={200: {"content": {"text/csv": {}}}})
def export_csv(db: DB, user: CurrentUser, event_id: str, kind: str, method: str | None = None):
    event = event_service.get_event(db, event_id)
    text = exports.export(db, user, event, kind, method)
    filename = f"{event.slug}-{kind}.csv"
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})
