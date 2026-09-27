"""T1: organizers create events with dates, tracks, prizes and questions."""

from doggfather import audit
from doggfather.db import fetch_all, fetch_one, fetch_value
from doggfather.services.events import get_event_by_slug
from tests.helpers import post_form

SLUG = "sample-hack-2026"


def new_event_form(**overrides):
    values = {
        "name": "Demo Night",
        "slug": "demo-night",
        "tagline": "One evening, one prize",
        "submissions_open_at": "2026-10-01T18:00",
        "submissions_close_at": "2026-10-03T18:00",
        "judging_close_at": "2026-10-08T18:00",
        "max_team_size": "3",
        "review_target": "3",
    }
    values.update(overrides)
    return values


def test_public_event_pages(as_role):
    visitor = as_role(None)
    listing = visitor.get("/events")
    assert listing.status_code == 200 and "Sample Hack 2026" in listing.text
    page = visitor.get(f"/events/{SLUG}")
    assert page.status_code == 200
    assert "Developer tools" in page.text and "Open hardware" in page.text


def test_organizer_creates_event_with_default_rubric(as_role, conn):
    org = as_role("organizer")
    response = post_form(org, "/organize/new", new_event_form(), token_from="/organize/new")
    assert response.status_code == 303 and response.headers["location"] == "/organize/demo-night"
    event = get_event_by_slug(conn, "demo-night")
    assert event.max_team_size == 3
    assert event.submissions_close_at == "2026-10-03T18:00:00Z"
    organizers = fetch_all(conn, "SELECT u.email FROM event_members m JOIN users u ON u.id = m.user_id"
                                 " WHERE m.event_id = ? AND m.role = 'organizer'", (event.id,))
    assert [r["email"] for r in organizers] == ["organizer@doggfather.local"]
    assert fetch_value(conn, "SELECT COUNT(*) FROM criteria WHERE event_id = ?", (event.id,)) == 3


def test_event_dates_are_validated(as_role):
    org = as_role("organizer")
    bad = new_event_form(submissions_close_at="2026-09-30T18:00")
    response = post_form(org, "/organize/new", bad, token_from="/organize/new")
    assert response.status_code == 422
    assert "must come after" in response.text


def test_only_organizers_can_create_or_manage(as_role):
    participant = as_role("participant")
    assert participant.get("/organize/new").status_code == 403
    judge = as_role("judge_a")
    assert judge.get(f"/organize/{SLUG}").status_code == 403
    assert judge.get(f"/organize/{SLUG}/settings").status_code == 403
    assert as_role("organizer").get(f"/organize/{SLUG}").status_code == 200


def test_tracks_prizes_and_questions(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/settings"
    assert post_form(org, f"/organize/{SLUG}/tracks", {"track_name": "Fintech"}, token_from=page).status_code == 303
    track_id = fetch_value(conn, "SELECT id FROM tracks WHERE name = 'Fintech'")
    post_form(org, f"/organize/{SLUG}/prizes", {"prize_name": "Best Fintech", "prize_value": "$300", "prize_track": track_id}, token_from=page)
    post_form(org, f"/organize/{SLUG}/questions", {"question_prompt": "Which APIs did you use?", "question_kind": "textarea",
                                                  "question_required": "on"}, token_from=page)
    prize = fetch_one(conn, "SELECT * FROM prizes WHERE name = 'Best Fintech'")
    assert prize["track_id"] == track_id and prize["value"] == "$300"
    question = fetch_one(conn, "SELECT * FROM custom_questions WHERE prompt = 'Which APIs did you use?'")
    assert question["kind"] == "textarea" and question["required"] == 1
    # A track that has projects cannot be removed out from under them.
    response = post_form(org, f"/organize/{SLUG}/tracks/trk_01/delete", token_from=page)
    assert response.status_code == 303
    assert fetch_value(conn, "SELECT COUNT(*) FROM tracks WHERE id = 'trk_01'") == 1
    post_form(org, f"/organize/{SLUG}/tracks/{track_id}/delete", token_from=page)
    assert fetch_value(conn, "SELECT COUNT(*) FROM tracks WHERE id = ?", (track_id,)) == 0


def test_settings_change_is_audited(as_role, conn):
    org = as_role("organizer")
    event = get_event_by_slug(conn, SLUG)
    values = {k: getattr(event, k) for k in ("name", "slug", "tagline", "description", "submissions_open_at",
                                            "submissions_close_at", "judging_close_at", "max_team_size", "review_target")}
    values["tagline"] = "Now with a new tagline"
    response = post_form(org, f"/organize/{SLUG}/settings", values, token_from=f"/organize/{SLUG}/settings")
    assert response.status_code == 303
    latest = audit.entries(conn, event_id=event.id, limit=1)[0]
    assert latest["action"] == "event.update" and "tagline" in latest["sentence"]


def test_phase_controls_move_boundaries(as_role, conn):
    org = as_role("organizer")
    post_form(org, "/organize/new", new_event_form(submissions_open_at="2026-01-01T00:00",
                                                    submissions_close_at="2027-01-01T00:00",
                                                    judging_close_at="2027-02-01T00:00"), token_from="/organize/new")
    assert get_event_by_slug(conn, "demo-night").submissions_open()
    post_form(org, "/organize/demo-night/phase", {"action": "close_submissions"}, token_from="/organize/demo-night")
    event = get_event_by_slug(conn, "demo-night")
    assert event.submissions_closed() and event.judging_open()
