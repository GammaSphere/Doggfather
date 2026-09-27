"""Per-request context: one database connection per HTTP request.

This is a plain ASGI middleware rather than ``BaseHTTPMiddleware`` so the
connection outlives streamed response bodies and is always closed, even when
the client disconnects halfway.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from . import db

SKIP_PREFIXES = ("/static/",)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        settings = scope["app"].state.settings
        conn = db.connect(settings.database_path)
        state = scope.setdefault("state", {})
        state["db"] = conn
        try:
            await self.app(scope, receive, send)
        finally:
            conn.close()
