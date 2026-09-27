"""Participant pages: your team for an event and the invite-link flow."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..auth import CurrentUser, RequiredUser
from ..deps import DB, AppSettings, Form
from ..errors import AppError, NotAuthenticated, ValidationFailed
from ..services import events as event_service
from ..services import teams as team_service
from .templating import redirect, render

router = APIRouter(include_in_schema=False)


def _team_page(request: Request, db, settings, user, event, *, errors=None, status_code=200):
    team = team_service.team_for_user(db, user.id, event.id)
    context = {"event": event, "team": team, "errors": errors or {}, "members": [], "invite_url": None}
    if team:
        context["members"] = team_service.members(db, team["id"])
        context["invite_url"] = f"{settings.base_url}/join/{team['invite_code']}"
        context["project"] = db.execute(
            "SELECT id, title, status, submitted_at, updated_at FROM projects WHERE team_id = ?"
            " ORDER BY status = 'submitted' DESC, updated_at DESC LIMIT 1", (team["id"],)).fetchone()
    return render(request, "teams/team.html", context, status_code=status_code)


@router.get("/events/{slug}/team")
def my_team(request: Request, db: DB, settings: AppSettings, user: RequiredUser, slug: str):
    return _team_page(request, db, settings, user, event_service.get_event_by_slug(db, slug))


@router.post("/events/{slug}/team")
def create_team(request: Request, db: DB, settings: AppSettings, user: RequiredUser, slug: str, form: Form):
    event = event_service.get_event_by_slug(db, slug)
    try:
        team_service.create_team(db, user, event, str(form.get("team_name") or ""))
    except ValidationFailed as exc:
        return _team_page(request, db, settings, user, event, errors=exc.fields, status_code=422)
    except AppError as exc:
        return redirect(request, f"/events/{slug}/team", exc.message, "error")
    return redirect(request, f"/events/{slug}/team", "Team created. Share the invite link with your teammates.")


@router.get("/join/{code}")
def join_page(request: Request, db: DB, user: CurrentUser, code: str):
    team = team_service.get_team_by_code(db, code)
    event = event_service.get_event(db, team["event_id"])
    if user is None:
        raise NotAuthenticated("Log in or create an account to join this team.")
    return render(request, "teams/join.html", {
        "team": team, "event": event, "code": code,
        "members": team_service.members(db, team["id"]),
        "current": team_service.team_for_user(db, user.id, event.id),
    })


@router.post("/join/{code}")
def join_submit(request: Request, db: DB, user: RequiredUser, code: str):
    team = team_service.get_team_by_code(db, code)
    event = event_service.get_event(db, team["event_id"])
    try:
        team_service.join_team(db, user, code)
    except AppError as exc:
        return redirect(request, f"/join/{code}", exc.message, "error")
    return redirect(request, f"/events/{event.slug}/team", f"Welcome to {team['name']}.")


def _team_action(request: Request, db, team_id: str, fn, success: str):
    team = team_service.get_team(db, team_id)
    event = event_service.get_event(db, team["event_id"])
    try:
        fn()
    except AppError as exc:
        return redirect(request, f"/events/{event.slug}/team", exc.message, "error")
    return redirect(request, f"/events/{event.slug}/team", success)


@router.post("/teams/{team_id}/invite")
def regenerate_invite(request: Request, db: DB, user: RequiredUser, team_id: str):
    return _team_action(request, db, team_id, lambda: team_service.regenerate_invite(db, user, team_id),
                        "New invite link created. The old one no longer works.")


@router.post("/teams/{team_id}/leave")
def leave(request: Request, db: DB, user: RequiredUser, team_id: str):
    return _team_action(request, db, team_id, lambda: team_service.leave_team(db, user, team_id), "You left the team.")


@router.post("/teams/{team_id}/members/{member_id}/remove")
def remove_member(request: Request, db: DB, user: RequiredUser, team_id: str, member_id: str):
    return _team_action(request, db, team_id, lambda: team_service.remove_member(db, user, team_id, member_id),
                        "Member removed.")


@router.post("/teams/{team_id}/rename")
def rename(request: Request, db: DB, user: RequiredUser, team_id: str, form: Form):
    return _team_action(request, db, team_id,
                        lambda: team_service.rename_team(db, user, team_id, str(form.get("team_name") or "")),
                        "Team renamed.")
