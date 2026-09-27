"""Community voting pages: the gate, the ballot, and organizer moderation."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import policy
from ..auth import CurrentUser, RequiredUser
from ..config import Settings
from ..deps import DB, AppSettings, Form, Limiter, client_ip
from ..errors import AppError, ValidationFailed
from ..security import keyed_hash, new_token, sign, unsign
from ..services import events as event_service
from ..services import voting
from ..services.voting import Voter
from .organizer import organizer_event
from .templating import redirect, render

router = APIRouter(include_in_schema=False)

VOTER_COOKIE = "voter"
DEVICE_COOKIE = "vdev"
PENDING_EMAIL_COOKIE = "vote_email"


def _ip_hash(request: Request, settings: Settings) -> str:
    return keyed_hash(settings.secret_key, client_ip(request), "vote-ip")


def _ua_hash(request: Request, settings: Settings) -> str:
    return keyed_hash(settings.secret_key, request.headers.get("user-agent", ""), "vote-ua")


def _cookie_voter(request: Request, db, settings: Settings, event) -> Voter | None:
    raw = unsign(settings.secret_key, request.cookies.get(VOTER_COOKIE), "voter")
    if not raw or ":" not in raw:
        return None
    event_id, voter_id = raw.split(":", 1)
    if event_id != event.id:
        return None
    return voting.get_voter(db, voter_id)


def resolve_voter(request: Request, db, settings: Settings, event, user, *, create: bool) -> Voter | None:
    """The ballot owner for this request, per the event's gate."""
    ip_hash = _ip_hash(request, settings)
    if event.voting_mode == "account":
        if user is None:
            return None
        if create:
            return voting.voter_for_account(db, event, user, ip_hash)
        row = db.execute("SELECT id FROM voters WHERE event_id = ? AND user_id = ?", (event.id, user.id)).fetchone()
        return voting.get_voter(db, row["id"]) if row else None
    if event.voting_mode == "email":
        voter = _cookie_voter(request, db, settings, event)
        if voter is None and user is not None:
            verified = db.execute("SELECT email_verified_at FROM users WHERE id = ?", (user.id,)).fetchone()
            if verified and verified[0]:
                voter = voting.voter_for_verified_email(db, event, user.email, ip_hash)
        return voter
    device = request.cookies.get(DEVICE_COOKIE)
    if device and create:
        return voting.voter_for_device(db, settings, event, device, ip_hash, _ua_hash(request, settings))
    if device:
        digest = keyed_hash(settings.secret_key, device, "vote-device")
        row = db.execute("SELECT id FROM voters WHERE event_id = ? AND device_hash = ?", (event.id, digest)).fetchone()
        return voting.get_voter(db, row["id"]) if row else None
    return None


@router.get("/vote/{slug}")
def ballot_page(request: Request, db: DB, settings: AppSettings, user: CurrentUser, slug: str):
    event = event_service.get_event_by_slug(db, slug)
    context = {"event": event, "modes": event_service.VOTING_MODES, "ballot": None, "gate": None, "errors": {}}
    if not event.voting_enabled or not event.voting_open():
        context["gate"] = "closed"
        return render(request, "vote/ballot.html", context)
    voter = resolve_voter(request, db, settings, event, user, create=False)
    set_device = None
    if event.voting_mode == "account" and user is None:
        context["gate"] = "login"
    elif event.voting_mode == "email" and voter is None:
        pending = unsign(settings.secret_key, request.cookies.get(PENDING_EMAIL_COOKIE), f"vote-email:{event.id}")
        context["gate"] = "code" if pending else "email"
        context["pending_email"] = pending
    else:
        # The shuffle seed is a stable identity, so the order never changes
        # between visits or after the first ballot is cast.
        if event.voting_mode == "account":
            seed_key = f"user:{user.id}"
        elif event.voting_mode == "email":
            seed_key = f"email:{voter.email}"
        else:
            seed_key = request.cookies.get(DEVICE_COOKIE) or ""
            if not seed_key:
                seed_key = set_device = new_token(18)
            seed_key = f"device:{seed_key}"
        context["ballot"] = voting.ballot(db, settings, event, voter, seed_key)
        context["voter"] = voter
    response = render(request, "vote/ballot.html", context)
    if set_device:
        response.set_cookie(DEVICE_COOKIE, set_device, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
                            secure=settings.cookie_secure, path="/")
    return response


@router.post("/vote/{slug}/code")
def request_code(request: Request, db: DB, settings: AppSettings, limiter: Limiter, slug: str, form: Form):
    event = event_service.get_event_by_slug(db, slug)
    email = str(form.get("email") or "").strip()
    limiter.enforce(f"vote-code:ip:{client_ip(request)}", 10, 3600)
    limiter.enforce(f"vote-code:email:{voting.normalize_voter_email(email)}", 3, 900,
                    "Too many codes for this address. Wait a few minutes.")
    try:
        voting.request_code(db, settings, event, email)
    except ValidationFailed as exc:
        return redirect(request, f"/vote/{slug}", next(iter(exc.fields.values()), exc.message), "error")
    response = redirect(request, f"/vote/{slug}", "Code sent. Check your inbox.")
    response.set_cookie(PENDING_EMAIL_COOKIE, sign(settings.secret_key, email, f"vote-email:{event.id}"),
                        max_age=900, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")
    return response


@router.post("/vote/{slug}/verify")
def verify_code(request: Request, db: DB, settings: AppSettings, limiter: Limiter, slug: str, form: Form):
    event = event_service.get_event_by_slug(db, slug)
    limiter.enforce(f"vote-verify:ip:{client_ip(request)}", 20, 900)
    email = unsign(settings.secret_key, request.cookies.get(PENDING_EMAIL_COOKIE), f"vote-email:{event.id}")
    if not email:
        return redirect(request, f"/vote/{slug}", "Start again: enter your email to get a code.", "error")
    try:
        voter = voting.verify_code(db, event, email, str(form.get("code") or ""), _ip_hash(request, settings))
    except ValidationFailed as exc:
        return redirect(request, f"/vote/{slug}", exc.fields.get("code", exc.message), "error")
    except AppError as exc:
        response = redirect(request, f"/vote/{slug}", exc.message, "error")
        response.delete_cookie(PENDING_EMAIL_COOKIE, path="/")
        return response
    response = redirect(request, f"/vote/{slug}", "Verified. Spend your credits wisely.")
    response.set_cookie(VOTER_COOKIE, sign(settings.secret_key, f"{event.id}:{voter.id}", "voter"),
                        max_age=60 * 60 * 24 * 60, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")
    response.delete_cookie(PENDING_EMAIL_COOKIE, path="/")
    return response


@router.post("/vote/{slug}/ballot")
def cast(request: Request, db: DB, settings: AppSettings, user: CurrentUser, limiter: Limiter, slug: str, form: Form):
    event = event_service.get_event_by_slug(db, slug)
    limiter.enforce(f"vote-cast:ip:{client_ip(request)}", 30, 60)
    try:
        voting.assert_open(event)
        voter = resolve_voter(request, db, settings, event, user, create=True)
        if voter is None:
            return redirect(request, f"/vote/{slug}", "Verify first, then vote.", "error")
        allocation = {key[2:]: form.get(key) or 0 for key in form.keys() if key.startswith("v_")}
        voting.cast_ballot(db, event, voter, allocation)
    except ValidationFailed as exc:
        return redirect(request, f"/vote/{slug}", exc.message, "error")
    except AppError as exc:
        return redirect(request, f"/vote/{slug}", exc.message, "error")
    return redirect(request, f"/vote/{slug}", "Ballot recorded. You can change it until voting closes.")


# ------------------------------------------------------- organizer: votes

@router.get("/organize/{slug}/votes")
def votes_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    voters = voting.voters_overview(db, user, event)
    return render(request, "organize/votes.html", {
        "event": event, "tab": "votes", "tally": voting.tally(db, event) if event.voting_enabled else [],
        "voters": voters, "flagged": sum(1 for v in voters if v["flagged"]),
    })


@router.post("/organize/{slug}/votes/{voter_id}")
def void_voter(request: Request, db: DB, user: RequiredUser, slug: str, voter_id: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        voting.set_voided(db, user, event, voter_id, form.get("action") == "void")
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/votes", exc.message, "error")
    return redirect(request, f"/organize/{slug}/votes", "Voter updated; the tally reflects it immediately.")


@router.get("/api/events/{event_id}/tally", tags=["voting"], include_in_schema=True,
            summary="Community vote tally (organizers live; public after close and publication)")
def tally_api(db: DB, user: CurrentUser, event_id: str):
    event = event_service.get_event(db, event_id)
    rows = voting.visible_tally(db, user, event)
    return {"event": {"id": event.id, "name": event.name}, "mode": event.voting_mode,
            "credits": event.vote_credits, "live": policy.is_organizer(db, user, event.id) and not event.tally_public,
            "results": rows}
