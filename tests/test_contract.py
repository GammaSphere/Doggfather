"""End to end: the official DOGFOOD checker against a real HTTP server.

This boots the seeded portal on a free port with uvicorn, points the
unmodified ``run.py`` at it using the repository's own ``.dogfood.toml``,
and requires every check to pass. If this test is green, the committed
acceptance report can be reproduced.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time

import pytest
import uvicorn

from doggfather.app import create_app
from tests.conftest import FIXTURES, ROOT


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(seeded_settings):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(seeded_settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def load_checker():
    spec = importlib.util.spec_from_file_location("dogfood_run", ROOT / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_checker_passes_every_check(live_server):
    run = load_checker()
    config = run.parse_toml((ROOT / ".dogfood.toml").read_text(encoding="utf-8"))
    config["portal"]["base_url"] = live_server
    checks = run.build_checks(config, FIXTURES)
    failures = [(c.tier, c.label, c.detail) for c in checks if not c.ok]
    assert not failures
    assert {c.tier for c in checks} == {"T1", "T2"} and len(checks) == 7


def test_claims_match_what_the_checker_can_verify():
    run = load_checker()
    config = run.parse_toml((ROOT / ".dogfood.toml").read_text(encoding="utf-8"))
    assert config["tiers"]["claimed"][:2] == ["T1", "T2"]
