"""REST API v1: everything the UI does, as JSON.

Authentication: ``Authorization: Bearer dgf_...`` (personal tokens, created on
your dashboard) or the browser session. Every handler delegates to the same
service function the HTML route uses, so permissions, deadlines and
validation are identical by construction. Errors share one envelope:

    {"error": "submissions_closed", "message": "...", ...details}
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import audit, policy
from ..auth import CurrentUser, RequiredUser, User
from ..deps import DB, AppSettings, Limiter, Payload
from ..errors import Forbidden, NotFound
from ..services import (
    assignment, bundles, comments, duplicates, exports, gallery, judges, normalization, progress, projects, results,
    rubric,
    scoring, teams, tokens, voting, webhooks,
)
from ..services import events as event_service
from ..services.events import Event

router = APIRouter(prefix="/api/v1")

# ------------------------------------------------------------------ models


class EventIn(BaseModel):
    name: str
    slug: str = ""
    tagline: str = ""
    description: str = ""
    submissions_open_at: str = Field(examples=["2026-09-26T18:00:00Z"])
    submissions_close_at: str = Field(examples=["2026-09-29T18:00:00Z"])
    judging_close_at: str = Field(examples=["2026-10-08T18:00:00Z"])
    max_team_size: int = 4
    review_target: int = 3
    voting_mode: Literal["off", "link", "email", "account"] = "off"
    voting_open_at: str | None = None
    voting_close_at: str | None = None
    vote_credits: int = 25


class EventPatch(BaseModel):
    name: str | None = None
    slug: str | None = None
    tagline: str | None = None
    description: str | None = None
    submissions_open_at: str | None = None
    submissions_close_at: str | None = None
    judging_close_at: str | None = None
    max_team_size: int | None = None
    review_target: int | None = None
    voting_mode: Literal["off", "link", "email", "account"] | None = None
    voting_open_at: str | None = None
    voting_close_at: str | None = None
    vote_credits: int | None = None


class PhaseIn(BaseModel):
    action: Literal["open_submissions", "close_submissions", "close_judging", "extend_judging"]


class TrackIn(BaseModel):
    name: str
    description: str = ""


class PrizeIn(BaseModel):
    name: str
    value: str = ""
    description: str = ""
    track_id: str | None = None


class QuestionIn(BaseModel):
    prompt: str
    kind: Literal["text", "textarea", "url", "choice"] = "text"
    required: bool = False
    options: list[str] = []
    help: str = ""


class EmailIn(BaseModel):
    email: str


class NameIn(BaseModel):
    name: str


class JoinIn(BaseModel):
    code: str


class ProjectIn(BaseModel):
    title: str
    tagline: str = ""
    description: str = ""
    track_id: str | None = None
    repo_url: str = ""
    demo_url: str = ""
    video_url: str = ""
    tags: list[str] = []
    answers: dict[str, str] = Field(default_factory=dict, description="question id -> answer")


class ProjectPatch(BaseModel):
    title: str | None = None
    tagline: str | None = None
    description: str | None = None
    track_id: str | None = None
    repo_url: str | None = None
    demo_url: str | None = None
    video_url: str | None = None
    tags: list[str] | None = None
    answers: dict[str, str] | None = None


class CommentIn(BaseModel):
    body: str


class ReasonIn(BaseModel):
    reason: str = ""


class CriterionIn(BaseModel):
    id: str | None = None
    label: str
    description: str = ""
    weight: float = 1.0
    min_score: int = 1
    max_score: int = 5
    delete: bool = False


class RubricIn(BaseModel):
    criteria: list[CriterionIn]
    track_weights: dict[str, dict[str, float | None]] = Field(
        default_factory=dict, description="track id -> criterion id -> weight (null clears the override)")


class InviteIn(BaseModel):
    email: str
    tracks: list[str] = []
    note: str = ""


class TracksIn(BaseModel):
    tracks: list[str]


class AutoAssignIn(BaseModel):
    target: int | None = None


class BatchIn(BaseModel):
    judge_id: str
    project_ids: list[str]


class ScoreIn(BaseModel):
    criteria: dict[str, int] = Field(description="criterion key (or id) -> score")
    comment: str = ""


class BallotIn(BaseModel):
    votes: dict[str, int] = Field(description="project id -> votes; costs votes^2 credits each")


class MethodIn(BaseModel):
    method: Literal["raw", "zscore", "bias", "pairwise"]


class WebhookIn(BaseModel):
    url: str
    topics: list[str] = ["*"]


# ------------------------------------------------------------- serializers

def event_json(db, event: Event, *, detail: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {k: getattr(event, k) for k in (
        "id", "slug", "name", "tagline", "description", "submissions_open_at", "submissions_close_at",
        "judging_close_at", "max_team_size", "review_target", "normalization", "results_published_at",
        "voting_mode", "voting_open_at", "voting_close_at", "vote_credits")}
    data["phase"] = event.phase
    data["submissions_open"] = event.submissions_open()
    data["voting_open"] = event.voting_open()
    if detail:
        data["tracks"] = [dict(t) for t in event_service.list_tracks(db, event.id)]
        data["prizes"] = [dict(p) for p in event_service.list_prizes(db, event.id)]
        data["questions"] = event_service.list_questions(db, event.id)
    return data


def project_json(db, project_id: str) -> dict[str, Any]:
    detail = projects.project_detail(db, project_id)
    data = projects.serialize(detail, detail["tags"])
    data.update({
        "team_name": detail["team_name"], "track_name": detail["track_name"],
        "members": [m["name"] for m in detail["members"]],
        "images": [{"id": i["id"], "url": f"/uploads/{i['file']}", "caption": i["caption"]} for i in detail["images"]],
        "answers": {a["id"]: a["answer"] for a in detail["answers"] if a["answer"]},
        "duplicate_of": detail["duplicate_of"],
    })
    return data


def _project_payload(body: BaseModel) -> Payload:
    data = body.model_dump(exclude_unset=True)
    answers = data.pop("answers", None) or {}
    data.update({f"q_{qid}": value for qid, value in answers.items()})
    if "track_id" in data and data["track_id"] is None:
        data["track_id"] = ""
    return Payload(data, is_json=True)


def _organizer_event(db, user: User | None, event_id: str) -> Event:
    event = event_service.get_event(db, event_id)
    policy.require_organizer(db, user, event.id)
    return event


# ----------------------------------------------------------------- account

@router.get("/me", tags=["account"], summary="Who am I, and what can I do where")
def me(db: DB, user: RequiredUser):
    roles = db.execute("SELECT event_id, role FROM event_members WHERE user_id = ? ORDER BY event_id, role",
                       (user.id,)).fetchall()
    return {"id": user.id, "name": user.name, "email": user.email, "is_admin": user.is_admin,
            "roles": [dict(r) for r in roles]}


@router.get("/me/tokens", tags=["account"], summary="List your API tokens")
def list_my_tokens(db: DB, user: RequiredUser):
    return [dict(t) for t in tokens.list_tokens(db, user)]


@router.post("/me/tokens", tags=["account"], status_code=201, summary="Create an API token (shown once)")
def create_token(db: DB, user: RequiredUser, body: NameIn):
    token_id, token = tokens.create(db, user, body.name)
    return {"id": token_id, "token": token, "note": "Store it now; it is not shown again."}


@router.delete("/me/tokens/{token_id}", tags=["account"], status_code=204, summary="Revoke an API token")
def revoke_token(db: DB, user: RequiredUser, token_id: str):
    tokens.revoke(db, user, token_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ events

@router.get("/events", tags=["events"], summary="List events")
def list_events(db: DB):
    return [{**event_json(db, item["event"], detail=False), "project_count": item["project_count"]}
            for item in event_service.list_events(db)]


@router.post("/events", tags=["events"], status_code=201, summary="Create an event (admins and organizers)")
def create_event(db: DB, user: RequiredUser, body: EventIn):
    if not event_service.can_create_events(db, user):
        raise Forbidden("Ask an admin to make you an organizer first.")
    return event_json(db, event_service.create_event(db, user, body.model_dump()))


@router.get("/events/{event_id}", tags=["events"], summary="Event with tracks, prizes and questions")
def get_event(db: DB, event_id: str):
    return event_json(db, event_service.get_event(db, event_id))


@router.patch("/events/{event_id}", tags=["events"], summary="Update event settings (partial)")
def patch_event(db: DB, user: RequiredUser, event_id: str, body: EventPatch):
    event = _organizer_event(db, user, event_id)
    values = {k: getattr(event, k) for k in event_service.EDITABLE}
    values.update(body.model_dump(exclude_unset=True))
    return event_json(db, event_service.update_event(db, user, event, values))


@router.post("/events/{event_id}/phase", tags=["events"], summary="Move a phase boundary to now")
def move_phase(db: DB, user: RequiredUser, event_id: str, body: PhaseIn):
    event = _organizer_event(db, user, event_id)
    return event_json(db, event_service.apply_phase_action(db, user, event, body.action))


@router.post("/events/{event_id}/tracks", tags=["events"], status_code=201, summary="Add a track")
def add_track(db: DB, user: RequiredUser, event_id: str, body: TrackIn):
    event = _organizer_event(db, user, event_id)
    return {"id": event_service.add_track(db, user, event, body.name, body.description)}


@router.delete("/events/{event_id}/tracks/{track_id}", tags=["events"], status_code=204, summary="Remove a track")
def remove_track(db: DB, user: RequiredUser, event_id: str, track_id: str):
    event_service.remove_track(db, user, _organizer_event(db, user, event_id), track_id)
    return Response(status_code=204)


@router.post("/events/{event_id}/prizes", tags=["events"], status_code=201, summary="Add a prize")
def add_prize(db: DB, user: RequiredUser, event_id: str, body: PrizeIn):
    event = _organizer_event(db, user, event_id)
    return {"id": event_service.add_prize(db, user, event, body.name, body.value, body.description, body.track_id)}


@router.delete("/events/{event_id}/prizes/{prize_id}", tags=["events"], status_code=204, summary="Remove a prize")
def remove_prize(db: DB, user: RequiredUser, event_id: str, prize_id: str):
    event_service.remove_prize(db, user, _organizer_event(db, user, event_id), prize_id)
    return Response(status_code=204)


@router.post("/events/{event_id}/questions", tags=["events"], status_code=201, summary="Add a submission question")
def add_question(db: DB, user: RequiredUser, event_id: str, body: QuestionIn):
    event = _organizer_event(db, user, event_id)
    return {"id": event_service.add_question(db, user, event, body.prompt, body.kind, body.required, body.options, body.help)}


@router.delete("/events/{event_id}/questions/{question_id}", tags=["events"], status_code=204, summary="Remove a question")
def remove_question(db: DB, user: RequiredUser, event_id: str, question_id: str):
    event_service.remove_question(db, user, _organizer_event(db, user, event_id), question_id)
    return Response(status_code=204)


@router.post("/events/{event_id}/organizers", tags=["events"], status_code=201, summary="Add a co-organizer")
def add_organizer(db: DB, user: RequiredUser, event_id: str, body: EmailIn):
    person = event_service.add_organizer(db, user, _organizer_event(db, user, event_id), body.email)
    return {"id": person.id, "email": person.email}


# ------------------------------------------------------------------- teams

def _team_json(db, team) -> dict[str, Any]:
    return {"id": team["id"], "event_id": team["event_id"], "name": team["name"], "invite_code": team["invite_code"],
            "members": [dict(m) for m in teams.members(db, team["id"])]}


@router.get("/events/{event_id}/team", tags=["teams"], summary="Your team in this event")
def my_team(db: DB, user: RequiredUser, event_id: str):
    team = teams.team_for_user(db, user.id, event_service.get_event(db, event_id).id)
    if team is None:
        raise NotFound("You are not on a team in this event.")
    return _team_json(db, team)


@router.post("/events/{event_id}/teams", tags=["teams"], status_code=201, summary="Form a team")
def create_team(db: DB, user: RequiredUser, event_id: str, body: NameIn):
    team_id = teams.create_team(db, user, event_service.get_event(db, event_id), body.name)
    return _team_json(db, teams.get_team(db, team_id))


@router.post("/teams/join", tags=["teams"], summary="Join a team with its invite code")
def join_team(db: DB, user: RequiredUser, body: JoinIn):
    return _team_json(db, teams.join_team(db, user, body.code))


@router.post("/teams/{team_id}/leave", tags=["teams"], status_code=204, summary="Leave a team")
def leave_team(db: DB, user: RequiredUser, team_id: str):
    teams.leave_team(db, user, team_id)
    return Response(status_code=204)


@router.delete("/teams/{team_id}/members/{user_id}", tags=["teams"], status_code=204, summary="Remove a member (captain)")
def remove_member(db: DB, user: RequiredUser, team_id: str, user_id: str):
    teams.remove_member(db, user, team_id, user_id)
    return Response(status_code=204)


@router.post("/teams/{team_id}/invite", tags=["teams"], summary="Rotate the invite code (captain)")
def rotate_invite(db: DB, user: RequiredUser, team_id: str):
    return {"invite_code": teams.regenerate_invite(db, user, team_id)}


# ---------------------------------------------------------------- projects

@router.get("/projects", tags=["projects"], summary="Search the public gallery")
def search_projects(db: DB, q: str = "", event: list[str] = Query(default=[]), track: list[str] = Query(default=[]),
                    tag: list[str] = Query(default=[]), sort: str = "recent", page: int = 1, per: int = 60):
    query = gallery.GalleryQuery(q, event, track, tag, sort, page, per).normalized()
    items, total = gallery.search(db, query)
    return {"total": total, "page": query.page, "per_page": query.per_page, "items": items}


@router.get("/projects/{project_id}", tags=["projects"], summary="One project (drafts: team and organizers only)")
def get_project(db: DB, user: CurrentUser, project_id: str):
    project = projects.get_project(db, project_id)
    if project["status"] != "submitted" and not (projects.is_team_member(db, user, project)
                                                 or policy.is_organizer(db, user, project["event_id"])):
        raise NotFound("No such project.")
    return project_json(db, project_id)


@router.post("/events/{event_id}/projects", tags=["projects"], status_code=201, summary="Start a draft for your team")
def create_project(db: DB, user: RequiredUser, limiter: Limiter, event_id: str, body: ProjectIn):
    limiter.enforce(f"project-write:{user.id}", 60, 60)
    project_id = projects.create_project(db, user, event_service.get_event(db, event_id), _project_payload(body))
    return project_json(db, project_id)


@router.patch("/projects/{project_id}", tags=["projects"], summary="Edit a project (until the deadline)")
def patch_project(db: DB, user: RequiredUser, limiter: Limiter, project_id: str, body: ProjectPatch):
    limiter.enforce(f"project-write:{user.id}", 60, 60)
    projects.update_project(db, user, project_id, _project_payload(body))
    return project_json(db, project_id)


@router.post("/projects/{project_id}/submit", tags=["projects"], summary="Submit to the gallery")
def submit_project(db: DB, user: RequiredUser, project_id: str):
    projects.submit_project(db, user, project_id)
    return project_json(db, project_id)


@router.post("/projects/{project_id}/unsubmit", tags=["projects"], summary="Return to draft")
def unsubmit_project(db: DB, user: RequiredUser, project_id: str):
    projects.unsubmit_project(db, user, project_id)
    return project_json(db, project_id)


@router.put("/projects/{project_id}/thumbnail", tags=["projects"], summary="Upload the thumbnail (multipart)")
def put_thumbnail(db: DB, settings: AppSettings, user: RequiredUser, project_id: str, image: UploadFile = File(...)):
    projects.set_thumbnail(db, settings, user, project_id, image)
    return project_json(db, project_id)


@router.post("/projects/{project_id}/images", tags=["projects"], status_code=201, summary="Add a gallery image (multipart)")
def add_image(db: DB, settings: AppSettings, user: RequiredUser, project_id: str, image: UploadFile = File(...),
              caption: str = Form("")):
    return {"id": projects.add_image(db, settings, user, project_id, image, caption)}


@router.delete("/projects/{project_id}/images/{image_id}", tags=["projects"], status_code=204, summary="Remove an image")
def delete_image(db: DB, settings: AppSettings, user: RequiredUser, project_id: str, image_id: str):
    projects.remove_image(db, settings, user, project_id, image_id)
    return Response(status_code=204)


@router.get("/projects/{project_id}/comments", tags=["projects"], summary="Visible comments")
def list_comments(db: DB, user: CurrentUser, project_id: str):
    project = projects.get_project(db, project_id)
    if project["status"] != "submitted":
        raise NotFound("No such project.")
    include_hidden = policy.is_organizer(db, user, project["event_id"])
    return [dict(c) for c in comments.list_comments(db, project_id, include_hidden=include_hidden)]


@router.post("/projects/{project_id}/comments", tags=["projects"], status_code=201, summary="Comment on a project")
def post_comment(db: DB, user: RequiredUser, limiter: Limiter, project_id: str, body: CommentIn):
    limiter.enforce(f"comment:user:{user.id}", 5, 60)
    return {"id": comments.add_comment(db, user, project_id, body.body)}


@router.post("/comments/{comment_id}/hide", tags=["moderation"], summary="Hide a comment (organizers)")
def hide_comment(db: DB, user: RequiredUser, comment_id: str, body: ReasonIn):
    comments.set_hidden(db, user, comment_id, True, body.reason)
    return {"id": comment_id, "hidden": True}


@router.post("/comments/{comment_id}/restore", tags=["moderation"], summary="Restore a comment (organizers)")
def restore_comment(db: DB, user: RequiredUser, comment_id: str):
    comments.set_hidden(db, user, comment_id, False)
    return {"id": comment_id, "hidden": False}


# ------------------------------------------------------- judging: organizer

@router.get("/events/{event_id}/rubric", tags=["judging"], summary="Rubric with weights and track overrides")
def get_rubric(db: DB, event_id: str):
    event = event_service.get_event(db, event_id)
    return {"criteria": [dict(c) for c in rubric.list_criteria(db, event.id)],
            "track_weights": rubric.track_overrides(db, event.id),
            "locked": rubric.scoring_started(db, event.id)}


@router.put("/events/{event_id}/rubric", tags=["judging"], summary="Replace the rubric (organizers)")
def put_rubric(db: DB, user: RequiredUser, event_id: str, body: RubricIn):
    event = _organizer_event(db, user, event_id)
    overrides = {(cid, track_id): ("" if weight is None else weight)
                 for track_id, weights in body.track_weights.items() for cid, weight in weights.items()}
    rubric.update_rubric(db, user, event, [c.model_dump() for c in body.criteria], overrides)
    return get_rubric(db, event_id)


@router.get("/events/{event_id}/judges", tags=["judging"], summary="Judge roster (organizers)")
def list_judges(db: DB, user: RequiredUser, event_id: str):
    return judges.roster(db, _organizer_event(db, user, event_id).id)


@router.post("/events/{event_id}/judges/invitations", tags=["judging"], status_code=201, summary="Invite a judge")
def invite_judge(db: DB, settings: AppSettings, user: RequiredUser, event_id: str, body: InviteIn):
    event = _organizer_event(db, user, event_id)
    judges.invite_judge(db, settings, user, event, body.email, body.tracks, body.note)
    return {"email": body.email, "status": "invited"}


@router.put("/events/{event_id}/judges/{judge_id}/tracks", tags=["judging"], summary="Set a judge's tracks")
def set_judge_tracks(db: DB, user: RequiredUser, event_id: str, judge_id: str, body: TracksIn):
    judges.set_tracks(db, user, _organizer_event(db, user, event_id), judge_id, body.tracks)
    return {"judge_id": judge_id, "tracks": body.tracks}


@router.delete("/events/{event_id}/judges/{judge_id}", tags=["judging"], status_code=204, summary="Remove a judge")
def remove_judge(db: DB, user: RequiredUser, event_id: str, judge_id: str):
    judges.remove_judge(db, user, _organizer_event(db, user, event_id), judge_id)
    return Response(status_code=204)


@router.get("/events/{event_id}/assignments", tags=["judging"], summary="Coverage matrix (organizers)")
def list_assignments(db: DB, user: RequiredUser, event_id: str):
    return assignment.matrix(db, _organizer_event(db, user, event_id).id)


@router.post("/events/{event_id}/assignments/auto", tags=["judging"], summary="Run automatic assignment")
def auto_assign(db: DB, user: RequiredUser, event_id: str, body: AutoAssignIn):
    plan = assignment.run_auto(db, user, _organizer_event(db, user, event_id), body.target)
    return {"batch": plan.batch, "created": [{"judge_id": j, "project_id": p} for j, p in plan.created],
            "shortfall": plan.shortfall}


@router.post("/events/{event_id}/assignments", tags=["judging"], status_code=201, summary="Assign a manual batch")
def batch_assign(db: DB, user: RequiredUser, event_id: str, body: BatchIn):
    batch = assignment.assign_batch(db, user, _organizer_event(db, user, event_id), body.judge_id, body.project_ids)
    return {"batch": batch}


@router.delete("/events/{event_id}/assignments/{assignment_id}", tags=["judging"], status_code=204,
               summary="Remove a pending assignment")
def unassign(db: DB, user: RequiredUser, event_id: str, assignment_id: str):
    assignment.unassign(db, user, _organizer_event(db, user, event_id), assignment_id)
    return Response(status_code=204)


@router.get("/events/{event_id}/progress", tags=["judging"], summary="Live judging progress (organizers)")
def event_progress(db: DB, user: RequiredUser, event_id: str):
    return progress.snapshot(db, _organizer_event(db, user, event_id))


@router.get("/events/{event_id}/results", tags=["results"], summary="Ranked results")
def event_results(db: DB, user: CurrentUser, event_id: str, method: str | None = None):
    event = event_service.get_event(db, event_id)
    if policy.is_organizer(db, user, event.id):
        return results.serialize(results.compute_table(db, event, method), include_methods=True)
    return results.serialize(results.visible_table(db, user, event), include_methods=False)


@router.put("/events/{event_id}/results/method", tags=["results"], summary="Choose the ranking method")
def set_method(db: DB, user: RequiredUser, event_id: str, body: MethodIn):
    event = results.set_method(db, user, _organizer_event(db, user, event_id), body.method)
    return {"method": event.normalization, "label": normalization.METHOD_LABELS[event.normalization]}


@router.post("/events/{event_id}/results/publish", tags=["results"], summary="Publish results")
def publish(db: DB, user: RequiredUser, event_id: str):
    event = results.publish(db, user, _organizer_event(db, user, event_id))
    return {"published_at": event.results_published_at}


@router.post("/events/{event_id}/results/unpublish", tags=["results"], summary="Hide results again")
def unpublish(db: DB, user: RequiredUser, event_id: str):
    results.unpublish(db, user, _organizer_event(db, user, event_id))
    return {"published_at": None}


@router.get("/events/{event_id}/export/{kind}.csv", tags=["exports"], response_class=Response,
            responses={200: {"content": {"text/csv": {}}}}, summary="CSV export (organizers)")
def export_csv(db: DB, user: CurrentUser, event_id: str, kind: str, method: str | None = None):
    event = event_service.get_event(db, event_id)
    return Response(exports.export(db, user, event, kind, method), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{event.slug}-{kind}.csv"'})


# ----------------------------------------------------------- judging: judge

@router.get("/judge/queue", tags=["judging"], summary="Your assignments in one event")
def judge_queue(db: DB, user: RequiredUser, event: str):
    ev = event_service.get_event(db, event)
    policy.require_judge(db, user, ev.id)
    return scoring.queue(db, user.id, ev.id)


@router.get("/judge/scores", tags=["judging"], summary="Your own scorecards")
def judge_scores(db: DB, user: RequiredUser, event: str | None = None):
    if not policy.is_judge_anywhere(db, user):
        raise Forbidden("Only judges have scorecards.", code="not_a_judge")
    return scoring.scores_for_judge(db, user.id, event)


@router.put("/judge/scores/{project_id}", tags=["judging"], summary="File or revise your scorecard")
def put_score(db: DB, user: RequiredUser, project_id: str, body: ScoreIn):
    scoring.save_score(db, user, project_id, body.criteria, body.comment)
    return scoring.own_score(db, user.id, project_id)


# ------------------------------------------------------------------ voting

@router.get("/events/{event_id}/ballot", tags=["voting"], summary="Your ballot (account-gated votes)")
def get_ballot(db: DB, settings: AppSettings, user: RequiredUser, event_id: str):
    event = _account_vote(db, event_id)
    voter = voting.voter_for_account(db, event, user, "api")
    return voting.ballot(db, settings, event, voter, f"user:{user.id}")


@router.put("/events/{event_id}/ballot", tags=["voting"], summary="Cast or replace your ballot (account-gated votes)")
def put_ballot(db: DB, user: RequiredUser, limiter: Limiter, event_id: str, body: BallotIn):
    event = _account_vote(db, event_id)
    limiter.enforce(f"vote-cast:user:{user.id}", 30, 60)
    voter = voting.voter_for_account(db, event, user, "api")
    return {"votes": voting.cast_ballot(db, event, voter, body.votes)}


def _account_vote(db, event_id: str) -> Event:
    event = event_service.get_event(db, event_id)
    voting.assert_open(event)
    if event.voting_mode != "account":
        raise Forbidden("This vote is gated by email or link; use the web ballot.", code="gate_not_supported")
    return event


@router.get("/events/{event_id}/tally", tags=["voting"], summary="Community tally (sealed until published)")
def get_tally(db: DB, user: CurrentUser, event_id: str):
    return voting.visible_tally(db, user, event_service.get_event(db, event_id))


# --------------------------------------------------------------- integrity

@router.get("/events/{event_id}/duplicates", tags=["moderation"], summary="Possible duplicate submissions")
def list_duplicates(db: DB, user: RequiredUser, event_id: str):
    return [dict(r) for r in duplicates.flagged(db, _organizer_event(db, user, event_id).id)]


@router.post("/events/{event_id}/duplicates/{project_id}/dismiss", tags=["moderation"], summary="Dismiss a duplicate flag")
def dismiss_duplicate(db: DB, user: RequiredUser, event_id: str, project_id: str):
    duplicates.dismiss(db, user, _organizer_event(db, user, event_id), project_id)
    return {"project_id": project_id, "dismissed": True}


@router.post("/events/{event_id}/projects/{project_id}/withdraw", tags=["moderation"], summary="Withdraw an entry")
def withdraw(db: DB, user: RequiredUser, event_id: str, project_id: str, body: ReasonIn):
    duplicates.withdraw(db, user, _organizer_event(db, user, event_id), project_id, body.reason)
    return {"project_id": project_id, "status": "withdrawn"}


@router.get("/events/{event_id}/audit", tags=["moderation"], summary="Audit trail in plain English (organizers)")
def event_audit(db: DB, user: RequiredUser, event_id: str, limit: int = Query(200, le=5000)):
    event = _organizer_event(db, user, event_id)
    check = audit.verify_chain(db)
    return {"chain_ok": check.ok, "checked": check.checked, "broken_at": check.broken_at,
            "entries": audit.entries(db, event_id=event.id, limit=limit)}


# ---------------------------------------------------------------- webhooks

@router.get("/events/{event_id}/webhooks", tags=["webhooks"], summary="Webhooks and delivery counts")
def list_webhooks(db: DB, user: RequiredUser, event_id: str):
    return {"topics": webhooks.TOPICS, "webhooks": webhooks.list_hooks(db, _organizer_event(db, user, event_id).id)}


@router.post("/events/{event_id}/webhooks", tags=["webhooks"], status_code=201, summary="Add a webhook")
def add_webhook(db: DB, settings: AppSettings, user: RequiredUser, event_id: str, body: WebhookIn):
    webhook_id, secret = webhooks.create(db, settings, user, _organizer_event(db, user, event_id), body.url, body.topics)
    return {"id": webhook_id, "secret": secret, "note": "Verify X-Doggfather-Signature with this secret."}


@router.delete("/events/{event_id}/webhooks/{webhook_id}", tags=["webhooks"], status_code=204, summary="Remove a webhook")
def delete_webhook(db: DB, user: RequiredUser, event_id: str, webhook_id: str):
    webhooks.delete(db, user, _organizer_event(db, user, event_id), webhook_id)
    return Response(status_code=204)


@router.get("/events/{event_id}/webhooks/deliveries", tags=["webhooks"], summary="Recent delivery attempts")
def webhook_deliveries(db: DB, user: RequiredUser, event_id: str):
    return [dict(r) for r in webhooks.deliveries(db, _organizer_event(db, user, event_id).id)]


# ----------------------------------------------------------------- bundles

@router.get("/events/{event_id}/bundle", tags=["bundles"], summary="Export the whole event as a bundle (organizers)")
def export_bundle(db: DB, user: RequiredUser, event_id: str):
    event = _organizer_event(db, user, event_id)
    data = bundles.export_bundle(db, event.id)
    audit.record(db, "export.bundle", actor=user, event_id=event.id, target_type="event", target_id=event.id)
    return data


@router.post("/bundles", tags=["bundles"], status_code=201, summary="Import an event bundle (fixtures.json shape)")
def import_bundle(db: DB, user: RequiredUser, data: dict[str, Any] = Body(...)):
    if not event_service.can_create_events(db, user):
        raise Forbidden("Only admins and organizers can import events.")
    report = bundles.import_bundle(db, data, actor=user)
    return {"event_id": report.event_id, "counts": report.counts, "warnings": report.warnings}
