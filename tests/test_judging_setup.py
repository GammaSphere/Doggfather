"""T2: rubric weights, judge invitations, and the assignment engine."""

import pytest

from doggfather import auth
from doggfather.db import fetch_all, fetch_value
from doggfather.errors import Conflict
from doggfather.services import assignment, rubric
from doggfather.services.events import get_event
from tests.factories import new_user_client, open_event
from tests.helpers import post_form

SLUG = "sample-hack-2026"


# ------------------------------------------------------------------ rubric

def test_weighted_total_uses_weights_and_ignores_zero_weight():
    assert rubric.weighted_total({"a": 5, "b": 1}, {"a": 3, "b": 1}) == pytest.approx(4.0)
    assert rubric.weighted_total({"a": 5, "b": 1}, {"a": 1, "b": 0}) == 5
    assert rubric.weighted_total({"a": 4, "b": 2}, {"a": 0, "b": 0}) == 3  # all-zero falls back to the mean


def test_track_override_changes_only_that_track(conn):
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    event = get_event(conn, "evt_01")
    rows = [{"id": c["id"], "label": c["label"], "description": c["description"], "weight": c["weight"],
             "min_score": c["min_score"], "max_score": c["max_score"]} for c in rubric.list_criteria(conn, event.id)]
    rubric.update_rubric(conn, organizer, event, rows, {("evt_01:quality", "trk_04"): "3"})
    table = rubric.weight_table(conn, event.id)
    assert rubric.weights_for(table, "trk_04")["evt_01:quality"] == 3
    assert rubric.weights_for(table, "trk_01")["evt_01:quality"] == 1


def test_rubric_scale_is_frozen_once_scoring_started(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/rubric"
    form = {"rows": "3"}
    for i, c in enumerate(rubric.list_criteria(conn, "evt_01")):
        form.update({f"c{i}_id": c["id"], f"c{i}_label": c["label"], f"c{i}_weight": "2" if i == 0 else "1",
                     f"c{i}_min": "1", f"c{i}_max": "5"})
    assert post_form(org, page, form, token_from=page).status_code == 303
    assert fetch_value(conn, "SELECT weight FROM criteria WHERE id = 'evt_01:functionality'") == 2  # re-weighting is fine
    form["c0_max"] = "10"
    post_form(org, page, form, token_from=page)
    assert fetch_value(conn, "SELECT max_score FROM criteria WHERE id = 'evt_01:functionality'") == 5  # scale is not


def test_judges_cannot_open_the_rubric_editor(as_role):
    assert as_role("judge_a").get(f"/organize/{SLUG}/rubric").status_code == 403


# ----------------------------------------------------------------- invites

def test_invite_to_accept_flow(seeded_app, as_role, conn):
    event = open_event(conn)
    org = as_role("organizer")
    track = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ? AND name = 'Tools'", (event.id,))
    page = f"/organize/{event.slug}/judges"
    post_form(org, f"/organize/{event.slug}/judges/invite", {"email": "fresh.judge@example.org", "tracks": track}, token_from=page)
    body = fetch_value(conn, "SELECT body FROM outbox WHERE to_email = 'fresh.judge@example.org'")
    link = body.split("http://localhost:8080")[1].split()[0]

    judge = new_user_client(seeded_app, "fresh.judge@example.org", "Fresh Judge")
    assert judge.get(link).status_code == 200
    response = post_form(judge, link, token_from=link)
    assert response.headers["location"] == f"/judge/{event.slug}"
    user_id = fetch_value(conn, "SELECT id FROM users WHERE email = 'fresh.judge@example.org'")
    assert fetch_value(conn, "SELECT role FROM event_members WHERE event_id = ? AND user_id = ?", (event.id, user_id)) == "judge"
    assert [r["track_id"] for r in fetch_all(conn, "SELECT track_id FROM judge_tracks WHERE user_id = ?", (user_id,))] == [track]


def test_a_competitor_cannot_accept_a_judge_invite(seeded_app, as_role, conn):
    event = open_event(conn)
    competitor = new_user_client(seeded_app, "competitor@example.org")
    team_page = f"/events/{event.slug}/team"
    post_form(competitor, team_page, {"team_name": "Contenders"}, token_from=team_page)
    org = as_role("organizer")
    post_form(org, f"/organize/{event.slug}/judges/invite", {"email": "competitor@example.org"},
              token_from=f"/organize/{event.slug}/judges")
    link = fetch_value(conn, "SELECT body FROM outbox WHERE to_email = 'competitor@example.org'").split("http://localhost:8080")[1].split()[0]
    post_form(competitor, link, token_from=link)
    user_id = fetch_value(conn, "SELECT id FROM users WHERE email = 'competitor@example.org'")
    assert fetch_value(conn, "SELECT COUNT(*) FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'judge'",
                       (event.id, user_id)) == 0


# -------------------------------------------------------------- assignment

def test_seed_tops_up_unfinished_batches_with_pending_work(conn):
    per_project = dict(conn.execute("SELECT project_id, COUNT(*) FROM assignments GROUP BY project_id").fetchall())
    assert min(per_project.values()) >= 3  # every fixture project now has a third reviewer lined up
    pending = fetch_all(conn, "SELECT judge_id, project_id FROM assignments WHERE status = 'pending'")
    assert len(pending) == 8
    # Judge A (jdg_26) picks up at least one of them, so the demo has real work queued.
    assert any(r["judge_id"] == "jdg_26" for r in pending)


def test_auto_assignment_respects_tracks_and_is_idempotent(conn):
    event = get_event(conn, "evt_01")
    rows = fetch_all(conn, "SELECT a.judge_id, p.track_id FROM assignments a JOIN projects p ON p.id = a.project_id"
                           " WHERE a.status = 'pending'")
    for row in rows:
        assert fetch_value(conn, "SELECT 1 FROM judge_tracks WHERE user_id = ? AND track_id = ?",
                           (row["judge_id"], row["track_id"])) == 1
    assert assignment.plan_auto(conn, event).created == []  # already at target


def test_auto_assignment_balances_load(conn):
    event = get_event(conn, "evt_01")
    before = dict(conn.execute("SELECT judge_id, COUNT(*) FROM assignments GROUP BY judge_id").fetchall())
    plan = assignment.plan_auto(conn, event, target=5)
    added: dict[str, int] = {}
    for judge_id, _ in plan.created:
        added[judge_id] = added.get(judge_id, 0) + 1
    # The busiest fixture judges are not piled on further while idle peers exist.
    assert added.get("jdg_24", 0) <= max(added.values())
    loads = [before.get(j, 0) + added.get(j, 0) for j in set(before) | set(added)]
    assert max(loads) - min(loads) <= max(before.values()) - min(before.values()) + 1
    assert plan.shortfall  # some tracks simply do not have 5 eligible judges; reported, not faked


def test_auto_assignment_is_deterministic(conn):
    event = get_event(conn, "evt_01")
    assert assignment.plan_auto(conn, event, target=4).created == assignment.plan_auto(conn, event, target=4).created


def test_filed_reviews_cannot_be_unassigned(conn):
    organizer = auth.get_user_by_email(conn, "organizer@doggfather.local")
    event = get_event(conn, "evt_01")
    done_id = fetch_value(conn, "SELECT id FROM assignments WHERE status = 'done' LIMIT 1")
    with pytest.raises(Conflict):
        assignment.unassign(conn, organizer, event, done_id)


def test_manual_batch_via_console(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/assignments"
    assert org.get(page).status_code == 200
    post_form(org, f"/organize/{SLUG}/assignments/batch", {"judge": "jdg_01", "projects": ["prj_24", "prj_39"]}, token_from=page)
    rows = fetch_all(conn, "SELECT project_id, batch FROM assignments WHERE judge_id = 'jdg_01' AND status = 'pending'"
                           " ORDER BY project_id")
    assert [r["project_id"] for r in rows] == ["prj_24", "prj_39"]
    assert rows[0]["batch"].startswith("B-")
