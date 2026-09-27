"""Operational endpoints: health checks for Docker and uptime monitors."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..db import fetch_value
from ..deps import DB

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="Liveness and database check")
def healthz(db: DB) -> dict:
    fetch_value(db, "SELECT 1")
    migrations = fetch_value(db, "SELECT COUNT(*) FROM schema_migrations", default=0)
    return {"status": "ok", "version": __version__, "migrations": migrations}
