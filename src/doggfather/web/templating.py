"""Jinja2 environment, page rendering and flash messages."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .. import __version__, clock
from ..config import Settings
from ..security import sign, unsign

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
FLASH_COOKIE = "flash"
TICKER = (
    "Build the platform that will judge you",
    "Self-hosted",
    "Offline first",
    "Weighted rubrics",
    "Documented normalization",
    "Open API",
)


# ------------------------------------------------------------------ filters

def _as_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return clock.parse(str(value))
    except ValueError:
        return None


def fmt_datetime(value: Any) -> str:
    moment = _as_datetime(value)
    return moment.strftime("%d %b %Y · %H:%M UTC").upper() if moment else "—"


def fmt_date(value: Any) -> str:
    moment = _as_datetime(value)
    return moment.strftime("%d %b %Y").upper() if moment else "—"


def fmt_input_datetime(value: Any) -> str:
    """Value for <input type="datetime-local">, in UTC."""
    moment = _as_datetime(value)
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else ""


def _span(seconds: float) -> str:
    seconds = int(abs(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


def fmt_relative(value: Any) -> str:
    moment = _as_datetime(value)
    if not moment:
        return "never"
    delta = (moment - clock.now()).total_seconds()
    return f"in {_span(delta)}" if delta > 0 else f"{_span(delta)} ago"


def fmt_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def paragraphs(text: str | None) -> Markup:
    """Escape user text and keep its paragraph breaks."""
    if not text:
        return Markup("")
    blocks = [block.strip() for block in str(text).replace("\r\n", "\n").split("\n\n")]
    html = "".join(f"<p>{escape(block).replace(chr(10), Markup('<br>'))}</p>" for block in blocks if block)
    return Markup(html)


def pct(part: float, whole: float) -> int:
    return int(round(100 * part / whole)) if whole else 0


def initials(name: str | None) -> str:
    words = [w for w in (name or "").split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def build_environment(settings: Settings) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml", "svg")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        dt=fmt_datetime,
        date=fmt_date,
        input_dt=fmt_input_datetime,
        ago=fmt_relative,
        num=fmt_number,
        paras=paragraphs,
        initials=initials,
        tojson_compact=lambda v: Markup(json.dumps(v, separators=(",", ":"))),
    )
    env.globals.update(
        version=__version__,
        app_name="Doggfather",
        base_url=settings.base_url,
        demo=settings.demo,
        pct=pct,
        ticker_items=TICKER,
    )
    return env


# ------------------------------------------------------------------ flashes

def add_flash(request: Request, response, message: str, kind: str = "ok") -> None:
    secret = request.app.state.settings.secret_key
    existing = read_flashes(request)
    existing.append({"kind": kind, "message": message})
    payload = json.dumps(existing[-4:], separators=(",", ":"))
    response.set_cookie(FLASH_COOKIE, sign(secret, _b64json(payload), "flash"),
                        httponly=True, samesite="lax", max_age=120, path="/")


def _b64json(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64json(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()


def read_flashes(request: Request) -> list[dict]:
    raw = unsign(request.app.state.settings.secret_key, request.cookies.get(FLASH_COOKIE), "flash")
    if not raw:
        return []
    try:
        data = json.loads(_unb64json(raw))
    except (ValueError, UnicodeDecodeError):
        return []
    return [d for d in data if isinstance(d, dict) and "message" in d]


def redirect(request: Request, url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        add_flash(request, response, message, kind)
    return response


# ---------------------------------------------------------------- rendering

def base_context(request: Request) -> dict[str, Any]:
    state = request.state
    user = getattr(state, "user", None)
    nav = getattr(state, "nav", None)
    if nav is None:
        nav = {"judge": False, "organizer": False, "admin": bool(user and user.is_admin)}
    return {
        "request": request,
        "user": user,
        "nav": nav,
        "csrf_token": getattr(state, "csrf", ""),
        "flashes": read_flashes(request),
        "now": clock.now_iso(),
        "path": request.url.path,
    }


def render(request: Request, template: str, context: dict[str, Any] | None = None, *,
           status_code: int = 200, headers: dict[str, str] | None = None) -> HTMLResponse:
    env: Environment = request.app.state.templates
    ctx = base_context(request)
    if context:
        ctx.update(context)
    html = env.get_template(template).render(ctx)
    response = HTMLResponse(html, status_code=status_code, headers=headers)
    if ctx["flashes"]:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response
