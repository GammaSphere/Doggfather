"""Shared fixtures: a fresh data directory and app per test."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doggfather.app import create_app
from doggfather.config import load_settings


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_dir=tmp_path / "data", secret_key="test-secret", demo=True, webhook_worker=False)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
