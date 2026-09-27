"""Application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db
from .config import Settings, load_settings
from .csrf import csrf_protect
from .errors import AppError
from .middleware import RequestContextMiddleware
from .ratelimit import RateLimiter
from .web import auth as auth_pages
from .web import events as event_pages
from .api import judging as judging_api
from .api import organizer as organizer_api
from .web import admin, gallery, integrity, judge, organizer, organizer_judging, projects, public, system, teams, vote
from .web.templating import build_environment, render

PACKAGE_DIR = Path(__file__).parent
log = logging.getLogger("doggfather")

HEADINGS = {
    400: "Bad request",
    401: "Log in first",
    403: "Access denied",
    404: "Signal lost",
    405: "Wrong method",
    409: "Conflict",
    422: "Check the form",
    429: "Slow down",
    500: "Something broke",
}


def wants_json(request: Request) -> bool:
    """API clients get JSON errors, browsers get pages."""
    path = request.url.path
    if path.startswith(("/api/", "/.well-known/")):
        return True
    if request.headers.get("content-type", "").startswith("application/json"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def error_response(request: Request, status: int, body: dict) -> Response:
    headers = {"Retry-After": str(body["retry_after"])} if "retry_after" in body else None
    if wants_json(request):
        return JSONResponse(body, status_code=status, headers=headers)
    if status == 401 and request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    context = {
        "status_code": status,
        "heading": HEADINGS.get(status, "Error"),
        "code": body.get("error", "error"),
        "message": body.get("message", ""),
    }
    return render(request, "error.html", context, status_code=status, headers=headers)


async def handle_app_error(request: Request, exc: AppError):
    return error_response(request, exc.status, exc.to_dict())


async def handle_http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    message = "Nothing lives at this address." if exc.status_code == 404 else str(exc.detail)
    return error_response(request, exc.status_code, {"error": code, "message": message})


async def handle_validation_error(request: Request, exc: RequestValidationError):
    fields = {".".join(str(p) for p in err["loc"][1:]) or "body": err["msg"] for err in exc.errors()}
    body = {"error": "validation_failed", "message": "Some fields need attention.", "fields": fields}
    return error_response(request, 422, body)


async def handle_unexpected(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    body = {"error": "internal_error", "message": "An unexpected error occurred. It has been logged."}
    try:
        return error_response(request, 500, body)
    except Exception:  # the error page itself failed; fall back to JSON
        return JSONResponse(body, status_code=500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(settings.database_path)) as conn:
        db.migrate(conn)

    app = FastAPI(
        title="Doggfather",
        version=__version__,
        summary="Self-hostable hackathon submission and judging platform.",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.templates = build_environment(settings)
    app.state.limiter = RateLimiter()

    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected)

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(system.router)
    app.include_router(judging_api.router)
    app.include_router(organizer_api.router)
    app.include_router(public.router)
    app.include_router(auth_pages.router)
    app.include_router(event_pages.router)
    app.include_router(organizer.router)
    app.include_router(organizer_judging.router)
    app.include_router(organizer_judging.invites)
    app.include_router(teams.router)
    app.include_router(judge.router)
    app.include_router(vote.router)
    app.include_router(admin.router)
    app.include_router(integrity.router)
    app.include_router(gallery.router)
    app.include_router(projects.router)
    return app
