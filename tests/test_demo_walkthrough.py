"""The Demo.md script, executed end to end.

Every act in Demo.md has a matching block here, doing what the presenter
clicks through the same routes. If this test passes, the demo works.
"""

import re
from datetime import timedelta

from doggfather import clock
from doggfather.db import fetch_value
from doggfather.services.events import get_event_by_slug
from tests.factories import new_user_client
from tests.helpers import post_form

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_demo_script_runs_end_to_end(seeded_app, as_role, conn):
    org = as_role("organizer")
    visitor = as_role(None)

    # Act 1: the public side of the seeded fixture event.
    assert "Glass Signal" in visitor.get("/projects").text
    assert "Dry Harbour" in visitor.get("/projects?q=harbour").text

    # Act 2: the organizer creates "Demo Night" and opens it.
    now = clock.now()
    response = post_form(org, "/organize/new", {
        "name": "Demo Night", "slug": "", "tagline": "One evening, one prize",
        "submissions_open_at": clock.iso(now + timedelta(hours=1))[:16],
        "submissions_close_at": clock.iso(now + timedelta(days=2))[:16],
        "judging_close_at": clock.iso(now + timedelta(days=9))[:16],
        "max_team_size": "4", "review_target": "3", "voting_mode": "off",
    }, token_from="/organize/new")
    assert response.headers["location"] == "/organize/demo-night"
    settings_page = "/organize/demo-night/settings"
    for track in ("AI tools", "Civic tech"):
        post_form(org, "/organize/demo-night/tracks", {"track_name": track}, token_from=settings_page)
    post_form(org, "/organize/demo-night/prizes", {"prize_name": "Best in show", "prize_value": "$500"}, token_from=settings_page)
    assert "Open submissions now" in org.get("/organize/demo-night").text
    post_form(org, "/organize/demo-night/phase", {"action": "open_submissions"}, token_from="/organize/demo-night")
    event = get_event_by_slug(conn, "demo-night")
    assert event.submissions_open()

    # Act 3: a participant forms a team, a teammate joins, they submit.
    sam = new_user_client(seeded_app, "sam@example.org", "Sam Builder")
    post_form(sam, "/events/demo-night/team", {"team_name": "Night Owls"}, token_from="/events/demo-night/team")
    invite = re.search(r"/join/([\w-]+)", sam.get("/events/demo-night/team").text).group(1)
    alex = new_user_client(seeded_app, "alex@example.org", "Alex Mate")
    post_form(alex, f"/join/{invite}", token_from=f"/join/{invite}")
    draft = post_form(sam, "/projects/new", {"event": event.id, "title": "Lamplight"},
                      token_from="/projects/new?event=demo-night")
    edit_page = draft.headers["location"]
    project_id = edit_page.split("/")[2]
    track_id = fetch_value(conn, "SELECT id FROM tracks WHERE event_id = ? AND name = 'AI tools'", (event.id,))
    submitted = post_form(sam, edit_page, {
        "title": "Lamplight", "tagline": "Finds the dark corners of your dashboards",
        "description": "Lamplight watches your dashboards overnight and tells you which ones went dark and why.",
        "track_id": track_id, "repo_url": "https://example.org/lamplight", "tags": "python fastapi",
        "action": "submit",
    }, token_from=edit_page, files={"thumbnail": ("cover.png", PNG, "image/png")})
    assert submitted.status_code == 303
    assert "Lamplight" in visitor.get(f"/projects?event={event.id}").text

    # Act 4: invite a judge, accept via the shown link, close submissions, assign, score.
    sent = post_form(org, "/organize/demo-night/judges/invite", {"email": "jonas.vogel@example.org", "tracks": track_id},
                     token_from="/organize/demo-night/judges")
    assert "Invitation sent to jonas.vogel@example.org" in sent.text
    invite_link = re.search(r'id="invite-link" value="[^"]*(/invite/[\w-]+)"', sent.text).group(1)
    assert invite_link in fetch_value(conn, "SELECT body FROM outbox WHERE to_email = 'jonas.vogel@example.org'"
                                            " ORDER BY id DESC LIMIT 1")
    jonas = as_role("judge_a")
    assert jonas.get(invite_link).status_code == 200
    post_form(jonas, invite_link, token_from=invite_link)
    post_form(org, "/organize/demo-night/phase", {"action": "close_submissions"}, token_from="/organize/demo-night")
    post_form(org, "/organize/demo-night/assignments/auto", {"target": "3"}, token_from="/organize/demo-night/assignments")
    queue = jonas.get("/judge/demo-night").text
    assert "Lamplight" in queue
    score_page = f"/judge/demo-night/projects/{project_id}"
    post_form(jonas, score_page, {f"c_{event.id}:functionality": "4", f"c_{event.id}:quality": "3",
                                  f"c_{event.id}:innovation": "5", "comment": "Useful and focused."}, token_from=score_page)
    assert fetch_value(conn, "SELECT COUNT(*) FROM scores WHERE project_id = ?", (project_id,)) == 1

    # Act 5: normalization on the fixture event, live progress, isolation.
    results = org.get("/organize/sample-hack-2026/results").text
    assert "Judge calibration" in results and "flat scorer" in results
    assert org.get("/organize/sample-hack-2026/progress").status_code == 200
    assert as_role("judge_b").get("/api/judges/jdg_26/scores").status_code == 403

    # Act 6: sealed until published, then public; certificates verify.
    assert visitor.get("/events/demo-night/results").status_code == 403
    post_form(org, "/organize/demo-night/phase", {"action": "close_judging"}, token_from="/organize/demo-night")
    post_form(org, "/organize/demo-night/results", {"action": "publish"}, token_from="/organize/demo-night/results")
    public = visitor.get("/events/demo-night/results")
    assert public.status_code == 200 and "Lamplight" in public.text
    issued = post_form(org, "/organize/demo-night/records", {"action": "certificates"}, token_from="/organize/demo-night/records")
    assert "Issued 2 certificates" in org.get(issued.headers["location"]).text
    record_id = fetch_value(conn, "SELECT id FROM records WHERE event_id = ? AND subject_name = 'Sam Builder'", (event.id,))
    assert "Signature valid" in visitor.get(f"/records/{record_id}").text
