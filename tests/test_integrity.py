"""T3 anti-abuse: duplicate detection, comments and moderation, audit trail."""

from doggfather import audit
from doggfather.db import fetch_one, fetch_value
from doggfather.services import duplicates
from tests.helpers import post_form

SLUG = "sample-hack-2026"


# ------------------------------------------------------------- duplicates

def test_fixture_duplicate_is_flagged_on_every_signal(conn):
    row = fetch_one(conn, "SELECT duplicate_of, duplicate_reason FROM projects WHERE id = 'prj_41'")
    assert row["duplicate_of"] == "prj_07"
    assert "same repository" in row["duplicate_reason"] and "same title" in row["duplicate_reason"]


def test_identical_one_line_summaries_do_not_flag_everything(conn):
    # All 41 fixture projects share the summary "One line of what it does."
    assert fetch_value(conn, "SELECT COUNT(*) FROM projects WHERE duplicate_of IS NOT NULL") == 1


def test_repo_normalization():
    assert duplicates.normalize_repo("HTTPS://www.GitHub.com/a/b.git/") == duplicates.normalize_repo("http://github.com/a/b")


def test_dismissed_flag_stays_dismissed(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/integrity"
    assert "Dry Harbour" in org.get(page).text
    post_form(org, f"/organize/{SLUG}/duplicates/prj_41/dismiss", token_from=page)
    post_form(org, f"/organize/{SLUG}/duplicates/scan", token_from=page)
    assert fetch_value(conn, "SELECT duplicate_of FROM projects WHERE id = 'prj_41'") is None


def test_withdrawn_duplicate_leaves_gallery_and_results(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/integrity"
    post_form(org, f"/organize/{SLUG}/projects/prj_41/withdraw", {"reason": "duplicate of prj_07"}, token_from=page)
    assert fetch_value(conn, "SELECT status FROM projects WHERE id = 'prj_41'") == "withdrawn"
    assert "/projects/prj_41" not in as_role(None).get("/projects").text
    ids = [r["id"] for r in org.get("/api/events/evt_01/results").json()["results"]]
    assert "prj_41" not in ids and len(ids) == 40


def test_integrity_page_is_organizer_only(as_role):
    assert as_role("judge_a").get(f"/organize/{SLUG}/integrity").status_code == 403


# --------------------------------------------------------------- comments

def test_logged_in_users_comment_and_text_is_escaped(as_role, conn):
    judge = as_role("judge_b")
    response = post_form(judge, "/projects/prj_05/comments", {"body": "<script>alert('x')</script> neat idea"},
                         token_from="/projects/prj_05")
    assert response.status_code == 303
    html = as_role(None).get("/projects/prj_05").text
    assert "&lt;script&gt;" in html and "<script>alert" not in html


def test_visitors_cannot_comment(as_role):
    visitor = as_role(None)
    response = post_form(visitor, "/projects/prj_05/comments", {"body": "hi"}, token_from="/login")
    assert response.status_code == 401


def test_comments_are_rate_limited(as_role):
    judge = as_role("judge_b")
    statuses = [post_form(judge, "/projects/prj_05/comments", {"body": f"thought {i}"}, token_from="/projects/prj_05").status_code
                for i in range(6)]
    assert statuses[:5] == [303] * 5 and statuses[5] == 429


def test_organizer_hides_and_restores_with_audit(as_role, conn):
    post_form(as_role("judge_b"), "/projects/prj_05/comments", {"body": "spam spam spam"}, token_from="/projects/prj_05")
    comment_id = fetch_value(conn, "SELECT id FROM comments")
    org = as_role("organizer")
    post_form(org, f"/comments/{comment_id}", {"action": "hide"}, token_from="/projects/prj_05")
    assert "spam spam spam" not in as_role(None).get("/projects/prj_05").text
    assert "spam spam spam" in org.get("/projects/prj_05").text  # organizers still see it, greyed
    post_form(org, f"/comments/{comment_id}", {"action": "restore"}, token_from="/projects/prj_05")
    actions = [e["action"] for e in audit.entries(conn, action_prefix="comment.", limit=5)]
    assert actions[:2] == ["comment.restore", "comment.hide"]


def test_participants_cannot_moderate(as_role, conn):
    post_form(as_role("judge_b"), "/projects/prj_05/comments", {"body": "fine comment"}, token_from="/projects/prj_05")
    comment_id = fetch_value(conn, "SELECT id FROM comments")
    response = post_form(as_role("participant"), f"/comments/{comment_id}", {"action": "hide"}, token_from="/projects/prj_05")
    assert response.status_code == 403
    assert fetch_value(conn, "SELECT hidden_at FROM comments") is None


# ------------------------------------------------------------ audit trail

def test_event_audit_trail_reads_like_sentences(as_role):
    as_role("participant").post("/projects/new", json={"title": "Too late", "summary": "x"})
    html = as_role("organizer").get(f"/organize/{SLUG}/audit").text
    assert "Chain intact" in html
    assert "Refused a change to “Too late”" in html
    assert "flagged as a possible duplicate" in html


def test_late_attempts_surface_on_the_integrity_page(as_role):
    as_role("participant").post("/projects/new", json={"title": "Sneaky edit", "summary": "x"})
    assert "Sneaky edit" in as_role("organizer").get(f"/organize/{SLUG}/integrity").text
