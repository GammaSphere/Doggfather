"""Small helpers shared by the test modules."""

from __future__ import annotations

import re

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(client, path: str = "/login") -> str:
    """Load a page and return the CSRF token embedded in its forms."""
    html = client.get(path).text
    match = CSRF_RE.search(html)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def post_form(client, path: str, data: dict | None = None, *, token_from: str = "/login", files=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", csrf_from(client, token_from))
    return client.post(path, data=payload, files=files, follow_redirects=False, **kwargs)


def register(client, email: str, name: str = "Test Person", password: str = "correct horse battery"):
    return post_form(client, "/register", {"email": email, "name": name, "password": password}, token_from="/register")


def login(client, email: str, password: str):
    return post_form(client, "/login", {"email": email, "password": password})
