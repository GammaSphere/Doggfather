"""Per-request context and response hardening.

For every dynamic request this middleware:

1. opens one SQLite connection (closed when the response is fully sent),
2. resolves the session cookie into ``request.state.user``,
3. makes sure the browser has a CSRF token cookie,
4. adds security headers to the response.

It is a plain ASGI middleware rather than ``BaseHTTPMiddleware`` so the
connection outlives streamed response bodies.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import auth, db
from .csrf import CSRF_COOKIE
from .security import new_token

SKIP_PREFIXES = ("/static/",)
EMBED_PREFIXES = ("/embed/",)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors {ancestors}"
)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        embeddable = path.startswith(EMBED_PREFIXES) or path == "/embed.js"
        if path.startswith(SKIP_PREFIXES):
            await self.app(scope, receive, self._harden(send, embeddable=False, cookie=None))
            return

        settings = scope["app"].state.settings
        conn = db.connect(settings.database_path)
        request = HTTPConnection(scope)
        state = scope.setdefault("state", {})
        state["db"] = conn
        try:
            user = None
            token = request.cookies.get(auth.SESSION_COOKIE)
            if token:
                user = auth.resolve_session(conn, settings, token)
            state["user"] = user
            state["session_token"] = token if user else None
            state["nav"] = auth.nav_for(conn, user)

            csrf = request.cookies.get(CSRF_COOKIE, "")
            new_cookie = None
            if len(csrf) < 20:
                csrf = new_token(24)
                new_cookie = (
                    f"{CSRF_COOKIE}={csrf}; Path=/; HttpOnly; SameSite=Lax"
                    + ("; Secure" if settings.cookie_secure else "")
                )
            state["csrf"] = csrf

            await self.app(scope, receive, self._harden(send, embeddable=embeddable, cookie=new_cookie))
        finally:
            conn.close()

    @staticmethod
    def _harden(send: Send, *, embeddable: bool, cookie: str | None) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("Content-Security-Policy", CSP.format(ancestors="*" if embeddable else "'none'"))
                if not embeddable:
                    headers.setdefault("X-Frame-Options", "DENY")
                if cookie:
                    headers.append("Set-Cookie", cookie)
            await send(message)

        return wrapped
