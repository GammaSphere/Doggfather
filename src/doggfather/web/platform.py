"""Developer-facing pages: API docs, personal tokens, organizer webhooks."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request

from ..auth import RequiredUser
from ..deps import DB, AppSettings, Form
from ..errors import AppError, ValidationFailed
from ..services import tokens, webhooks
from .organizer import organizer_event
from .templating import redirect, render

router = APIRouter(include_in_schema=False)
METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4}


@router.get("/api/docs")
def api_docs(request: Request):
    """Offline API reference rendered from the live OpenAPI document
    (no Swagger CDN: the portal must work with the network off)."""
    spec = request.app.openapi()
    groups: dict[str, list[dict]] = defaultdict(list)
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            body = op.get("requestBody", {}).get("content", {})
            schema_ref = next(iter(body.values()), {}).get("schema", {}).get("$ref", "") if body else ""
            groups[(op.get("tags") or ["other"])[0]].append({
                "method": method.upper(), "path": path, "summary": op.get("summary", ""),
                "params": [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"],
                "body": schema_ref.rsplit("/", 1)[-1],
            })
    for ops in groups.values():
        ops.sort(key=lambda o: (o["path"], METHOD_ORDER.get(o["method"].lower(), 9)))
    schemas = spec.get("components", {}).get("schemas", {})
    return render(request, "api_docs.html", {"groups": dict(sorted(groups.items())), "spec": spec,
                                             "schemas": {k: v for k, v in schemas.items() if k.endswith("In") or k.endswith("Patch")}})


# ------------------------------------------------------------------ tokens

@router.get("/me/tokens")
def tokens_page(request: Request, db: DB, user: RequiredUser):
    return render(request, "auth/tokens.html", {"tokens": tokens.list_tokens(db, user), "created": None, "errors": {}})


@router.post("/me/tokens")
def create_token(request: Request, db: DB, user: RequiredUser, form: Form):
    try:
        _, token = tokens.create(db, user, str(form.get("name") or ""))
    except ValidationFailed as exc:
        return render(request, "auth/tokens.html", {"tokens": tokens.list_tokens(db, user), "created": None,
                                                    "errors": exc.fields}, status_code=422)
    # Rendered, not redirected: the token is shown exactly once and never stored.
    return render(request, "auth/tokens.html", {"tokens": tokens.list_tokens(db, user), "created": token, "errors": {}})


@router.post("/me/tokens/{token_id}/revoke")
def revoke_token(request: Request, db: DB, user: RequiredUser, token_id: str):
    tokens.revoke(db, user, token_id)
    return redirect(request, "/me/tokens", "Token revoked. Anything using it stops working now.")


# ---------------------------------------------------------------- webhooks

def _webhooks_page(request: Request, db, event, *, created=None, errors=None, status_code=200):
    return render(request, "organize/webhooks.html", {
        "event": event, "tab": "webhooks", "hooks": webhooks.list_hooks(db, event.id),
        "deliveries": webhooks.deliveries(db, event.id), "topics": webhooks.TOPICS,
        "created": created, "errors": errors or {},
    }, status_code=status_code)


@router.get("/organize/{slug}/webhooks")
def webhooks_page(request: Request, db: DB, user: RequiredUser, slug: str):
    return _webhooks_page(request, db, organizer_event(db, user, slug))


@router.post("/organize/{slug}/webhooks")
def add_webhook(request: Request, db: DB, settings: AppSettings, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    try:
        webhook_id, secret = webhooks.create(db, settings, user, event, str(form.get("url") or ""),
                                             [str(t) for t in form.getlist("topics")] or ["*"])
    except ValidationFailed as exc:
        return _webhooks_page(request, db, event, errors=exc.fields, status_code=422)
    return _webhooks_page(request, db, event, created={"id": webhook_id, "secret": secret})


@router.post("/organize/{slug}/webhooks/{webhook_id}/delete")
def delete_webhook(request: Request, db: DB, user: RequiredUser, slug: str, webhook_id: str):
    webhooks.delete(db, user, organizer_event(db, user, slug), webhook_id)
    return redirect(request, f"/organize/{slug}/webhooks", "Webhook removed.")


@router.post("/organize/{slug}/webhooks/{webhook_id}/test")
def test_webhook(request: Request, db: DB, user: RequiredUser, slug: str, webhook_id: str):
    try:
        webhooks.send_test(db, user, organizer_event(db, user, slug), webhook_id)
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/webhooks", exc.message, "error")
    return redirect(request, f"/organize/{slug}/webhooks", "Ping queued; watch the delivery log.")
