"""Builders for state the fixtures do not contain (an event that is open now)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from doggfather import auth, clock
from doggfather.services import events as event_service
from tests.helpers import register

ORGANIZER = "organizer@doggfather.local"


def open_event(conn, *, slug: str = "open-hack", max_team_size: int = 3, tracks=("General", "Tools")):
    """An event whose submission window is open right now."""
    organizer = auth.get_user_by_email(conn, ORGANIZER)
    now = clock.now()
    event = event_service.create_event(conn, organizer, {
        "name": f"Open Hack {slug}",
        "slug": slug,
        "submissions_open_at": clock.iso(now - timedelta(hours=1)),
        "submissions_close_at": clock.iso(now + timedelta(days=2)),
        "judging_close_at": clock.iso(now + timedelta(days=9)),
        "max_team_size": str(max_team_size),
        "review_target": "2",
    })
    for name in tracks:
        event_service.add_track(conn, organizer, event, name)
    return event_service.get_event(conn, event.id)


def new_user_client(app, email: str, name: str = "New Person") -> TestClient:
    """A fresh browser session for a newly registered account."""
    client = TestClient(app)
    client.__enter__()
    response = register(client, email, name)
    assert response.status_code == 303, response.text
    return client
