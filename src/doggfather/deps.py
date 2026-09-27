"""FastAPI dependencies shared by the HTML and JSON routers."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, Request

from .config import Settings


def get_db(request: Request) -> sqlite3.Connection:
    return request.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


DB = Annotated[sqlite3.Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
