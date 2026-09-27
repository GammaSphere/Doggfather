"""Application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager, closing
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db
from .config import Settings, load_settings
from .errors import AppError
from .middleware import RequestContextMiddleware
from .web import system

PACKAGE_DIR = Path(__file__).parent


def wants_json(request: Request) -> bool:
    """API clients get JSON errors, browsers get pages."""
    path = request.url.path
    if path.startswith(("/api/", "/.well-known/")):
        return True
    if request.headers.get("content-type", "").startswith("application/json"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _error_response(request: Request, status: int, body: dict) -> JSONResponse | HTMLResponse:
    headers = {}
    if "retry_after" in body:
        headers["Retry-After"] = str(body["retry_after"])
    if wants_json(request):
        return JSONResponse(body, status_code=status, headers=headers)
    html = f"<h1>{status}</h1><p>{escape(body.get('message', ''))}</p>"
    return HTMLResponse(html, status_code=status, headers=headers)


async def handle_app_error(request: Request, exc: AppError):
    return _error_response(request, exc.status, exc.to_dict())


async def handle_http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error_response(request, exc.status_code, {"error": code, "message": str(exc.detail)})


async def handle_validation_error(request: Request, exc: RequestValidationError):
    fields = {".".join(str(p) for p in err["loc"][1:]) or "body": err["msg"] for err in exc.errors()}
    body = {"error": "validation_failed", "message": "Some fields need attention.", "fields": fields}
    return _error_response(request, 422, body)


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
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)

    app.include_router(system.router)
    return app
