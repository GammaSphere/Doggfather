"""FastAPI dependencies shared by the HTML and JSON routers."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import Depends, Request
from starlette.datastructures import FormData, UploadFile

from .config import Settings
from .ratelimit import RateLimiter


def get_db(request: Request) -> sqlite3.Connection:
    return request.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


async def get_form(request: Request) -> FormData:
    """Parsed form body, resolved on the event loop so sync endpoints can
    run in the threadpool without blocking it."""
    return await request.form()


class Payload:
    """One view over a JSON object or a form body, so a route can serve
    browsers and API clients alike (``/projects/new`` does)."""

    def __init__(self, data: Any, *, is_json: bool) -> None:
        self._data = data
        self.is_json = is_json

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default) if hasattr(self._data, "get") else default
        return value

    def text(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        if value is None or isinstance(value, UploadFile):
            return default
        return str(value).strip()

    def getlist(self, key: str) -> list[Any]:
        if self.is_json:
            value = self._data.get(key) if isinstance(self._data, dict) else None
            if value is None:
                return []
            return list(value) if isinstance(value, (list, tuple)) else [value]
        return list(self._data.getlist(key))

    def keys(self) -> list[str]:
        return list(self._data.keys()) if hasattr(self._data, "keys") else []

    def file(self, key: str) -> UploadFile | None:
        if self.is_json:
            return None
        value = self._data.get(key)
        return value if isinstance(value, UploadFile) and value.filename else None


async def get_payload(request: Request) -> Payload:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        return Payload(data if isinstance(data, dict) else {}, is_json=True)
    return Payload(await request.form(), is_json=False)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


DB = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Limiter = Annotated[RateLimiter, Depends(get_limiter)]
Form = Annotated[FormData, Depends(get_form)]
Body = Annotated[Payload, Depends(get_payload)]
