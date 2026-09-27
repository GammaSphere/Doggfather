"""Certificates, judge records, and public verification."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..auth import CurrentUser, RequiredUser
from ..deps import DB, AppSettings, Form
from ..errors import AppError
from ..services import records
from ..services import results as results_service
from .organizer import organizer_event
from .templating import redirect, render

router = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/v1", tags=["records"])


# --------------------------------------------------------------- public

@router.get("/.well-known/doggfather/signing-key.pem")
def public_key(settings: AppSettings):
    return PlainTextResponse(records.signer(settings).public_pem, media_type="application/x-pem-file")


@router.get("/records/{record_id}.json")
def record_json(db: DB, settings: AppSettings, record_id: str):
    return records.export(records.get_record(db, record_id), settings)


@router.get("/records/{record_id}")
def certificate(request: Request, db: DB, settings: AppSettings, record_id: str):
    check = records.verify_record(db, settings, record_id)
    if check.record is None:
        return render(request, "records/verify.html", {"check": check, "query": record_id}, status_code=404)
    return render(request, "records/certificate.html", {"r": check.record, "p": check.payload, "check": check,
                                                         "labels": records.KIND_LABELS})


@router.get("/verify")
def verify_page(request: Request):
    return render(request, "records/verify.html", {"check": None, "query": ""})


@router.post("/verify")
def verify_submit(request: Request, db: DB, settings: AppSettings, form: Form):
    query = str(form.get("record") or "").strip()
    if query.startswith("{"):
        try:
            data = json.loads(query)
            check = records.verify_payload(settings, data["payload"], data["signature"])
        except (ValueError, KeyError, TypeError):
            check = records.Verification(None, None, False, False, "That is not a record export (payload + signature).")
    else:
        record_id = query.rsplit("/", 1)[-1].removesuffix(".json")
        check = records.verify_record(db, settings, record_id)
    return render(request, "records/verify.html", {"check": check, "query": query})


# ------------------------------------------------------------------ api

@api.get("/signing-key", summary="The platform's public Ed25519 key")
def signing_key(settings: AppSettings):
    s = records.signer(settings)
    return {"algorithm": "Ed25519", "key_id": s.key_id, "public_key_pem": s.public_pem}


@api.get("/records/{record_id}", summary="A signed record, ready for offline verification")
def api_record(db: DB, settings: AppSettings, record_id: str):
    return records.export(records.get_record(db, record_id), settings)


@api.get("/records/{record_id}/verify", summary="Verify a record's signature and revocation status")
def api_verify(db: DB, settings: AppSettings, record_id: str):
    check = records.verify_record(db, settings, record_id)
    return {"valid": check.ok, "signature_valid": check.valid_signature, "revoked": check.revoked, "reason": check.reason}


@api.get("/records/{record_id}/reveal", summary="Disclose the scorecards behind a judge record (judge or organizers)")
def api_reveal(db: DB, user: CurrentUser, record_id: str):
    return records.reveal(db, user, record_id)


# ------------------------------------------------------------ organizer

@router.get("/organize/{slug}/records")
def records_page(request: Request, db: DB, user: RequiredUser, slug: str):
    event = organizer_event(db, user, slug)
    ranking = results_service.compute_table(db, event).rows
    return render(request, "organize/records.html", {
        "event": event, "tab": "records", "records": records.list_records(db, event.id),
        "prizes": records.prize_board(db, event.id), "ranking": ranking, "labels": records.KIND_LABELS,
    })


@router.post("/organize/{slug}/records")
def records_action(request: Request, db: DB, settings: AppSettings, user: RequiredUser, slug: str, form: Form):
    event = organizer_event(db, user, slug)
    action = str(form.get("action") or "")
    try:
        if action == "judges":
            message = f"Issued {records.issue_judge_records(db, settings, user, event)} judge records."
        elif action == "certificates":
            message = f"Issued {records.issue_certificates(db, settings, user, event)} certificates."
        elif action == "award":
            records.award_prize(db, user, event, str(form.get("prize") or ""), str(form.get("project") or "") or None)
            message = "Prize assignment saved."
        elif action == "revoke":
            records.revoke(db, user, str(form.get("record") or ""), str(form.get("reason") or ""))
            message = "Record revoked. Verification now reports it."
        else:
            message = "Nothing to do."
    except AppError as exc:
        return redirect(request, f"/organize/{slug}/records", exc.message, "error")
    return redirect(request, f"/organize/{slug}/records", message)

