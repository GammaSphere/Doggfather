"""Public pages: anyone, no session required."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..db import fetch_value
from ..deps import DB
from ..services.events import list_events
from .templating import render

router = APIRouter(include_in_schema=False)


def platform_stats(db) -> dict[str, int]:
    return {
        "events": fetch_value(db, "SELECT COUNT(*) FROM events", default=0),
        "projects": fetch_value(db, "SELECT COUNT(*) FROM projects WHERE status = 'submitted'", default=0),
        "teams": fetch_value(db, "SELECT COUNT(*) FROM teams", default=0),
        "judges": fetch_value(db, "SELECT COUNT(DISTINCT user_id) FROM event_members WHERE role = 'judge'", default=0),
        "scores": fetch_value(db, "SELECT COUNT(*) FROM scores", default=0),
    }


@router.get("/")
def home(request: Request, db: DB):
    events = [{**vars(item["event"]), "phase": item["event"].phase, "project_count": item["project_count"]}
              for item in list_events(db)[:6]]
    return render(request, "home.html", {"events": events, "stats": platform_stats(db)})
