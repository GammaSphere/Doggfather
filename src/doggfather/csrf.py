"""Cross-site request forgery protection.

Applied globally as a FastAPI dependency so no state-changing route can be
added without it:

- Form posts (urlencoded or multipart) must echo the per-browser token from
  the ``csrf`` cookie in a ``csrf_token`` field (double-submit cookie).
- JSON requests pass without a token: browsers cannot send
  ``application/json`` cross-site without a CORS preflight, which this
  server never approves.
- A present ``Origin`` header must match our own host in every case.
- Requests authenticated with an API bearer token carry no ambient
  credentials and are exempt.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from .errors import Forbidden
from .security import constant_equals

CSRF_COOKIE = "csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def _same_origin(origin: str, request: Request) -> bool:
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc.lower() == (request.headers.get("host") or "").lower()


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS or getattr(request.state, "via_bearer", False):
        return
    origin = request.headers.get("origin")
    if origin is not None and not _same_origin(origin, request):
        raise Forbidden("Cross-origin request refused.", code="csrf_failed")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        return
    supplied = request.headers.get("x-csrf-token", "")
    if content_type in FORM_TYPES:
        form = await request.form()
        supplied = str(form.get("csrf_token") or supplied)
    expected = getattr(request.state, "csrf", "")
    if not supplied or not expected or not constant_equals(supplied, expected):
        raise Forbidden("This form expired or came from another site. Reload the page and try again.",
                        code="csrf_failed")
