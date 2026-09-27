"""Shared fixtures.

``client`` is an empty portal. ``seeded_client`` is a portal loaded with the
official fixtures.json in demo mode, exactly as ``docker compose up`` boots
it. Seeding runs once per test session into a template database, which each
test then copies, so every test starts from identical state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from doggfather import db
from doggfather.app import create_app
from doggfather.config import load_settings
from doggfather.seed import DEMO_SESSIONS, seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))


def make_settings(data_dir: Path, **extra):
    return load_settings(data_dir=data_dir, secret_key="test-secret", demo=True, webhook_worker=False, **extra)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path / "data")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("template"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    seed(conn, settings, FIXTURES)
    conn.close()
    return settings.database_path


@pytest.fixture
def seeded_settings(tmp_path, seeded_template):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source = sqlite3.connect(seeded_template)
    target = sqlite3.connect(data_dir / "doggfather.db")
    source.backup(target)
    source.close()
    target.close()
    return make_settings(data_dir)


@pytest.fixture
def seeded_app(seeded_settings):
    return create_app(seeded_settings)


@pytest.fixture
def seeded_client(seeded_app):
    with TestClient(seeded_app) as test_client:
        yield test_client


@pytest.fixture
def conn(seeded_settings):
    connection = db.connect(seeded_settings.database_path)
    yield connection
    connection.close()


@pytest.fixture
def as_role(seeded_app):
    """Factory for clients authenticated as a demo role:
    ``as_role("judge_a").get(...)``. ``as_role(None)`` is a visitor."""
    clients = []

    def make(role: str | None) -> TestClient:
        test_client = TestClient(seeded_app)
        test_client.__enter__()
        if role:
            test_client.cookies.set("session", DEMO_SESSIONS[role])
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        test_client.__exit__(None, None, None)
