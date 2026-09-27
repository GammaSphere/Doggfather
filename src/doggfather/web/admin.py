"""Platform admin: accounts, the mail outbox, and the global audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import audit, policy
from ..auth import RequiredUser
from ..db import fetch_all, fetch_one, transaction
from ..deps import DB, Form
from ..errors import Conflict, NotFound
from ..services import mailer
from .templating import redirect, render

router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("")
def index(request: Request, db: DB, user: RequiredUser, q: str = ""):
    policy.require_admin(user)
    like = f"%{q.strip()}%"
    users = fetch_all(
        db,
        "SELECT u.*, (SELECT GROUP_CONCAT(DISTINCT role) FROM event_members WHERE user_id = u.id) AS roles"
        " FROM users u WHERE u.email LIKE ? OR u.name LIKE ? ORDER BY u.is_admin DESC, u.created_at DESC LIMIT 200",
        (like, like),
    )
    return render(request, "admin/index.html", {"users": users, "q": q, "chain": audit.verify_chain(db)})


@router.post("/users/{user_id}/admin")
def toggle_admin(request: Request, db: DB, user: RequiredUser, user_id: str, form: Form):
    policy.require_admin(user)
    target = fetch_one(db, "SELECT * FROM users WHERE id = ?", (user_id,))
    if target is None:
        raise NotFound("No such user.")
    value = 1 if form.get("value") == "1" else 0
    if target["id"] == user.id and value == 0:
        raise Conflict("You cannot remove your own admin rights.")
    with transaction(db):
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (value, user_id))
        audit.record(db, "admin.toggle", actor=user, target_type="user", target_id=user_id,
                     detail={"email": target["email"], "value": bool(value)})
    return redirect(request, "/admin", "Updated.")


@router.get("/outbox")
def outbox(request: Request, db: DB, user: RequiredUser, to: str = ""):
    policy.require_admin(user)
    return render(request, "admin/outbox.html", {"messages": mailer.recent(db, 200, to.strip() or None), "to": to})


@router.get("/audit")
def audit_page(request: Request, db: DB, user: RequiredUser):
    policy.require_admin(user)
    return render(request, "admin/audit.html", {"entries": audit.entries(db, limit=500), "chain": audit.verify_chain(db)})
