"""T4: certificates and signed, publicly verifiable judge records."""

import json
import sqlite3

import pytest

from doggfather import auth
from doggfather.db import fetch_value
from doggfather.errors import Conflict
from doggfather.services import records, signing
from doggfather.services.events import get_event
from doggfather.tools import verify_record
from tests.helpers import post_form

SLUG = "sample-hack-2026"


def close_judging(conn):
    conn.execute("UPDATE events SET judging_close_at = '2026-03-05T00:00:00Z' WHERE id = 'evt_01'")


@pytest.fixture
def organizer(conn):
    return auth.get_user_by_email(conn, "organizer@doggfather.local")


def test_sign_and_verify_round_trip_and_tamper_detection(seeded_settings):
    signer = records.signer(seeded_settings)
    payload = {"type": "demo", "n": 3, "name": "Ada"}
    signature = signer.sign(payload)
    assert signing.verify(signer.public_key, payload, signature)
    assert not signing.verify(signer.public_key, {**payload, "n": 4}, signature)
    other = signing.Signer(signing.Ed25519PrivateKey.generate())
    assert not signing.verify(other.public_key, payload, signature)


def test_judge_records_wait_for_judging_to_close(conn, seeded_settings, organizer):
    with pytest.raises(Conflict):
        records.issue_judge_records(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    close_judging(conn)
    assert records.issue_judge_records(conn, seeded_settings, organizer, get_event(conn, "evt_01")) == 30
    # Idempotent: nothing changed, nothing reissued.
    assert records.issue_judge_records(conn, seeded_settings, organizer, get_event(conn, "evt_01")) == 0


def test_judge_record_commits_to_scores_and_reveal_matches(conn, seeded_settings, organizer, as_role):
    close_judging(conn)
    records.issue_judge_records(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    record_id = fetch_value(conn, "SELECT id FROM records WHERE kind = 'judge_record' AND subject_user_id = 'jdg_26'")
    payload = json.loads(fetch_value(conn, "SELECT payload FROM records WHERE id = ?", (record_id,)))
    assert payload["reviews"] == 10 and "criteria" not in json.dumps(payload)  # scores are committed, not disclosed

    revealed = as_role("judge_a").get(f"/api/v1/records/{record_id}/reveal").json()
    assert revealed["matches"] is True and len(revealed["scorecards"]) == 10
    assert as_role("judge_b").get(f"/api/v1/records/{record_id}/reveal").status_code == 403
    assert as_role(None).get(f"/api/v1/records/{record_id}/reveal").status_code == 403


def test_certificates_for_every_submitting_team_member(conn, seeded_settings, organizer):
    issued = records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    members = fetch_value(conn, "SELECT COUNT(DISTINCT m.user_id) FROM team_members m JOIN projects p ON p.team_id = m.team_id"
                                " WHERE p.status = 'submitted' AND p.event_id = 'evt_01'")
    assert issued == members


def test_awards_need_published_results(conn, seeded_settings, organizer):
    event = get_event(conn, "evt_01")
    prize = conn.execute("INSERT INTO prizes (id, event_id, name, value) VALUES ('prz_1', 'evt_01', 'Grand Prize', '$800')")
    records.award_prize(conn, organizer, event, "prz_1", "prj_08")
    with pytest.raises(Conflict):
        records.issue_certificates(conn, seeded_settings, organizer, event)
    conn.execute("UPDATE events SET results_published_at = '2026-03-10T00:00:00Z'")
    records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    award = json.loads(fetch_value(conn, "SELECT payload FROM records WHERE kind = 'award' LIMIT 1"))
    assert award["prize"]["name"] == "Grand Prize" and award["project"]["id"] == "prj_08"
    assert prize is not None


def test_public_verification_and_revocation(conn, seeded_settings, organizer, as_role):
    records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    record_id = fetch_value(conn, "SELECT id FROM records LIMIT 1")
    visitor = as_role(None)
    page = visitor.get(f"/records/{record_id}")
    assert page.status_code == 200 and "Signature valid" in page.text
    assert visitor.get(f"/api/v1/records/{record_id}/verify").json()["valid"] is True
    assert "Genuine" in post_form(visitor, "/verify", {"record": record_id}, token_from="/verify").text

    records.revoke(conn, organizer, record_id, "issued to the wrong person")
    verdict = visitor.get(f"/api/v1/records/{record_id}/verify").json()
    assert verdict["valid"] is False and verdict["revoked"] is True and verdict["signature_valid"] is True


def test_pasted_forgery_is_rejected(conn, seeded_settings, organizer, as_role):
    records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    record_id = fetch_value(conn, "SELECT id FROM records LIMIT 1")
    export = as_role(None).get(f"/records/{record_id}.json").json()
    export["payload"]["project"]["title"] = "Something I did not build"
    html = post_form(as_role(None), "/verify", {"record": json.dumps(export)}, token_from="/verify").text
    assert "does not match" in html


def test_records_are_immutable_in_the_database(conn, seeded_settings, organizer):
    records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE records SET payload = '{}'")


def test_offline_verifier_tool(conn, seeded_settings, organizer, as_role, tmp_path, capsys):
    records.issue_certificates(conn, seeded_settings, organizer, get_event(conn, "evt_01"))
    record_id = fetch_value(conn, "SELECT id FROM records LIMIT 1")
    visitor = as_role(None)
    (tmp_path / "record.json").write_text(visitor.get(f"/records/{record_id}.json").text, encoding="utf-8")
    (tmp_path / "key.pem").write_text(visitor.get("/.well-known/doggfather/signing-key.pem").text, encoding="utf-8")
    assert verify_record.main([str(tmp_path / "record.json"), str(tmp_path / "key.pem")]) == 0
    assert "VALID" in capsys.readouterr().out
    tampered = json.loads((tmp_path / "record.json").read_text())
    tampered["payload"]["person"]["name"] = "Mallory"
    (tmp_path / "record.json").write_text(json.dumps(tampered), encoding="utf-8")
    assert verify_record.main([str(tmp_path / "record.json"), str(tmp_path / "key.pem")]) == 1


def test_records_console_is_organizer_only(as_role):
    assert as_role("organizer").get(f"/organize/{SLUG}/records").status_code == 200
    assert as_role("judge_a").get(f"/organize/{SLUG}/records").status_code == 403
