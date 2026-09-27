"""Accounts and server-side sessions.

The cookie carries a random 256-bit token; the database stores only its
SHA-256. Sessions slide: each use pushes expiry out, up to the configured
lifetime. Logging in always mints a fresh token (no session fixation), and
a password reset revokes every session the account has.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import Response

from . import clock
from .config import Settings
from .db import fetch_one, fetch_value, new_id, transaction
from .errors import NotAuthenticated, ValidationFailed
from .security import hash_password, new_token, token_hash, verify_password

SESSION_COOKIE = "session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
TOUCH_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str
    is_admin: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(id=row["id"], email=row["email"], name=row["name"], is_admin=bool(row["is_admin"]))

    @property
    def first_name(self) -> str:
        return self.name.split(" ")[0] if self.name else self.email

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>"


# ------------------------------------------------------------- validation

def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str | None:
    if not email or len(email) > 254 or not EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def validate_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 200:
        return "That password is too long."
    return None


def validate_name(name: str) -> str | None:
    if not name.strip():
        return "Tell us what to call you."
    if len(name) > 80:
        return "Keep it under 80 characters."
    return None


# ----------------------------------------------------------------- users

def get_user(db: sqlite3.Connection, user_id: str) -> User | None:
    row = fetch_one(db, "SELECT * FROM users WHERE id = ?", (user_id,))
    return User.from_row(row) if row else None


def get_user_by_email(db: sqlite3.Connection, email: str) -> User | None:
    row = fetch_one(db, "SELECT * FROM users WHERE email = ?", (normalize_email(email),))
    return User.from_row(row) if row else None


def ensure_user(db: sqlite3.Connection, email: str, name: str, *, user_id: str | None = None,
                password: str | None = None, verified: bool = False) -> User:
    """Return the account for an email, creating a password-less one if needed.

    Used by imports and invitations: people can exist (as team members or
    judges) before they ever log in, and claim the account later via a
    password reset link.
    """
    email = normalize_email(email)
    existing = get_user_by_email(db, email)
    if existing:
        return existing
    uid = user_id or new_id("usr")
    now = clock.now_iso()
    db.execute(
        "INSERT INTO users (id, email, name, password_hash, email_verified_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, email, name.strip() or email.split("@")[0], hash_password(password) if password else None,
         now if verified else None, now),
    )
    return User(id=uid, email=email, name=name.strip() or email.split("@")[0])


def register(db: sqlite3.Connection, email: str, name: str, password: str) -> User:
    email = normalize_email(email)
    errors = {k: v for k, v in {
        "email": validate_email(email),
        "name": validate_name(name),
        "password": validate_password(password),
    }.items() if v}
    if not errors:
        row = fetch_one(db, "SELECT password_hash FROM users WHERE email = ?", (email,))
        if row is not None:
            errors["email"] = (
                "An account already exists for this email. Log in, or use "
                "'forgot password' to claim an account an organizer created for you."
            )
    if errors:
        raise ValidationFailed(fields=errors)
    return ensure_user(db, email, name, password=password)


def authenticate(db: sqlite3.Connection, email: str, password: str) -> User | None:
    row = fetch_one(db, "SELECT * FROM users WHERE email = ?", (normalize_email(email),))
    # verify_password runs even for unknown emails so timing does not leak existence.
    if not verify_password(password, row["password_hash"] if row else None) or row is None:
        return None
    return User.from_row(row)


def set_password(db: sqlite3.Connection, user_id: str, password: str) -> None:
    error = validate_password(password)
    if error:
        raise ValidationFailed(fields={"password": error})
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))


# -------------------------------------------------------------- sessions

def create_session(db: sqlite3.Connection, settings: Settings, user_id: str,
                   ip: str | None = None, user_agent: str | None = None, *, token: str | None = None,
                   lifetime: timedelta | None = None) -> str:
    token = token or new_token()
    now = clock.now()
    expires = now + (lifetime or timedelta(days=settings.session_days))
    db.execute(
        "INSERT OR REPLACE INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at, ip, user_agent)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_hash(token), user_id, clock.iso(now), clock.iso(now), clock.iso(expires), ip, (user_agent or "")[:200]),
    )
    return token


def resolve_session(db: sqlite3.Connection, settings: Settings, token: str) -> User | None:
    digest = token_hash(token)
    row = fetch_one(
        db,
        "SELECT s.expires_at, s.last_seen_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ?",
        (digest,),
    )
    if row is None:
        return None
    now = clock.now()
    if clock.parse(row["expires_at"]) <= now:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
        return None
    if now - clock.parse(row["last_seen_at"]) >= TOUCH_INTERVAL:
        # Slide the window, never past the configured lifetime from now.
        new_expiry = max(clock.parse(row["expires_at"]), now + timedelta(days=settings.session_days))
        db.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                   (clock.iso(now), clock.iso(new_expiry), digest))
    return User.from_row(row)


def end_session(db: sqlite3.Connection, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def end_all_sessions(db: sqlite3.Connection, user_id: str, keep_token: str | None = None) -> int:
    keep = token_hash(keep_token) if keep_token else ""
    cursor = db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?", (user_id, keep))
    return cursor.rowcount


def session_count(db: sqlite3.Connection, user_id: str) -> int:
    return fetch_value(db, "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,), default=0)


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_days * 86400, path="/",
        httponly=True, samesite="lax", secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# -------------------------------------------------------- password reset

def start_password_reset(db: sqlite3.Connection, email: str) -> tuple[User, str] | None:
    user = get_user_by_email(db, email)
    if not user:
        return None
    token = new_token()
    now = clock.now()
    db.execute(
        "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash(token), user.id, clock.iso(now), clock.iso(now + timedelta(hours=2))),
    )
    return user, token


def reset_user_for_token(db: sqlite3.Connection, token: str) -> User | None:
    row = fetch_one(
        db,
        "SELECT r.expires_at, r.used_at, u.* FROM password_resets r JOIN users u ON u.id = r.user_id"
        " WHERE r.token_hash = ?",
        (token_hash(token),),
    )
    if row is None or row["used_at"] or clock.parse(row["expires_at"]) <= clock.now():
        return None
    return User.from_row(row)


def complete_password_reset(db: sqlite3.Connection, token: str, password: str) -> User | None:
    with transaction(db):
        user = reset_user_for_token(db, token)
        if not user:
            return None
        set_password(db, user.id, password)
        db.execute("UPDATE password_resets SET used_at = ? WHERE token_hash = ?", (clock.now_iso(), token_hash(token)))
        # Whoever held the old password loses every session.
        end_all_sessions(db, user.id)
        # Proving control of the inbox verifies the address.
        db.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
                   (clock.now_iso(), user.id))
    return user


# ---------------------------------------------------- request dependencies

def current_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise NotAuthenticated()
    return user


CurrentUser = Annotated[User | None, Depends(current_user)]
RequiredUser = Annotated[User, Depends(require_user)]


def nav_for(db: sqlite3.Connection, user: User | None) -> dict[str, bool]:
    if user is None:
        return {"judge": False, "organizer": False, "admin": False}
    roles = {row[0] for row in db.execute("SELECT DISTINCT role FROM event_members WHERE user_id = ?", (user.id,))}
    return {"judge": "judge" in roles, "organizer": "organizer" in roles or user.is_admin, "admin": user.is_admin}
