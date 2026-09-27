"""Log in, register, log out, password reset, and the personal dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import audit, auth
from ..auth import CurrentUser, RequiredUser
from ..deps import DB, AppSettings, Form, Limiter, client_ip
from ..errors import ValidationFailed
from ..services import mailer
from .templating import redirect, render

router = APIRouter(include_in_schema=False)


def safe_next(target: str | None, default: str = "/me") -> str:
    """Only same-site relative paths, so ?next= cannot become an open redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target


def _demo_accounts(request: Request) -> list[dict]:
    if not request.app.state.settings.demo:
        return []
    from ..seed import demo_accounts

    return demo_accounts()


# ------------------------------------------------------------------ login

@router.get("/login")
def login_page(request: Request, user: CurrentUser, next: str = "/me"):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/login.html", {"next": safe_next(next), "email": "", "error": None,
                                               "demo_accounts": _demo_accounts(request)})


@router.post("/login")
def login_submit(request: Request, db: DB, settings: AppSettings, limiter: Limiter, form: Form):
    email = auth.normalize_email(str(form.get("email") or ""))
    password = str(form.get("password") or "")
    target = safe_next(str(form.get("next") or ""))
    ip = client_ip(request)
    limiter.enforce(f"login:ip:{ip}", 30, 300)
    limiter.enforce(f"login:email:{email}", 8, 900, "Too many attempts for this account. Wait a few minutes.")

    user = auth.authenticate(db, email, password)
    if user is None:
        audit.record(db, "auth.login_failed", actor_label=email or "unknown", detail={"email": email}, ip=ip)
        return render(request, "auth/login.html",
                      {"next": target, "email": email, "error": "That email and password do not match.",
                       "demo_accounts": _demo_accounts(request)}, status_code=400)

    auth.end_session(db, request.state.session_token)  # never reuse a pre-login session
    token = auth.create_session(db, settings, user.id, ip, request.headers.get("user-agent"))
    audit.record(db, "auth.login", actor=user, ip=ip)
    response = redirect(request, target, f"Welcome back, {user.first_name}.")
    auth.set_session_cookie(response, settings, token)
    return response


@router.post("/logout")
def logout(request: Request, db: DB, user: CurrentUser):
    auth.end_session(db, request.state.session_token)
    if user:
        audit.record(db, "auth.logout", actor=user, ip=client_ip(request))
    response = redirect(request, "/", "Logged out.")
    auth.clear_session_cookie(response)
    return response


# --------------------------------------------------------------- register

@router.get("/register")
def register_page(request: Request, user: CurrentUser, next: str = "/me"):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/register.html", {"next": safe_next(next), "values": {}, "errors": {}})


@router.post("/register")
def register_submit(request: Request, db: DB, settings: AppSettings, limiter: Limiter, form: Form):
    ip = client_ip(request)
    limiter.enforce(f"register:ip:{ip}", 10, 3600, "Too many new accounts from this address. Try again later.")
    values = {key: str(form.get(key) or "").strip() for key in ("email", "name")}
    target = safe_next(str(form.get("next") or ""))
    try:
        user = auth.register(db, values["email"], values["name"], str(form.get("password") or ""))
    except ValidationFailed as exc:
        return render(request, "auth/register.html", {"next": target, "values": values, "errors": exc.fields},
                      status_code=422)
    audit.record(db, "auth.register", actor=user, ip=ip)
    token = auth.create_session(db, settings, user.id, ip, request.headers.get("user-agent"))
    response = redirect(request, target, "Account created. You are logged in.")
    auth.set_session_cookie(response, settings, token)
    return response


# ---------------------------------------------------------- password reset

@router.get("/forgot")
def forgot_page(request: Request):
    return render(request, "auth/forgot.html", {"sent": False})


@router.post("/forgot")
def forgot_submit(request: Request, db: DB, settings: AppSettings, limiter: Limiter, form: Form):
    ip = client_ip(request)
    email = auth.normalize_email(str(form.get("email") or ""))
    limiter.enforce(f"forgot:ip:{ip}", 10, 3600)
    limiter.enforce(f"forgot:email:{email}", 3, 3600)
    started = auth.start_password_reset(db, email)
    audit.record(db, "auth.password_reset_requested", actor_label=email, detail={"email": email}, ip=ip)
    if started:
        user, token = started
        mailer.send(
            db, settings, user.email, "Set your Doggfather password",
            f"Hi {user.first_name},\n\nUse this link within two hours to set a new password:\n\n"
            f"{settings.base_url}/reset/{token}\n\nIf you did not ask for this, ignore this message.",
        )
    # Same answer either way: this form does not reveal who has an account.
    return render(request, "auth/forgot.html", {"sent": True})


@router.get("/reset/{token}")
def reset_page(request: Request, db: DB, token: str):
    user = auth.reset_user_for_token(db, token)
    return render(request, "auth/reset.html", {"token": token, "valid": user is not None, "errors": {}},
                  status_code=200 if user else 410)


@router.post("/reset/{token}")
def reset_submit(request: Request, db: DB, settings: AppSettings, token: str, form: Form):
    try:
        user = auth.complete_password_reset(db, token, str(form.get("password") or ""))
    except ValidationFailed as exc:
        return render(request, "auth/reset.html", {"token": token, "valid": True, "errors": exc.fields},
                      status_code=422)
    if user is None:
        return render(request, "auth/reset.html", {"token": token, "valid": False, "errors": {}}, status_code=410)
    ip = client_ip(request)
    audit.record(db, "auth.password_reset", actor=user, ip=ip)
    session = auth.create_session(db, settings, user.id, ip, request.headers.get("user-agent"))
    response = redirect(request, "/me", "Password set. Every other session was signed out.")
    auth.set_session_cookie(response, settings, session)
    return response


# ---------------------------------------------------------------- account

@router.get("/me")
def dashboard(request: Request, db: DB, user: RequiredUser):
    memberships = db.execute(
        "SELECT e.id, e.slug, e.name, e.submissions_close_at, m.role FROM event_members m"
        " JOIN events e ON e.id = m.event_id WHERE m.user_id = ? ORDER BY e.submissions_close_at DESC, m.role",
        (user.id,),
    ).fetchall()
    return render(request, "auth/me.html", {
        "memberships": memberships,
        "session_count": auth.session_count(db, user.id),
    })


@router.post("/me/logout-others")
def logout_others(request: Request, db: DB, user: RequiredUser):
    removed = auth.end_all_sessions(db, user.id, keep_token=request.state.session_token)
    audit.record(db, "auth.logout_all", actor=user, ip=client_ip(request))
    return redirect(request, "/me", f"Signed out {removed} other session{'s' if removed != 1 else ''}.")
