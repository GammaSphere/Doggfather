"""Judge score endpoints: the backend half of role isolation.

``GET /api/judge/scores``            the caller's own scorecards
``GET /api/judge/scores?judge=<id>`` someone's scorecards, if allowed
``GET /api/judges/<id>/scores``      same, as a path

Allowed: the judge themself, an organizer of an event that judge serves, an
admin. Everyone else gets 403, including other judges, and including
requests for judge ids that do not exist (a 404 there would let a judge
enumerate their peers).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from .. import policy
from ..auth import CurrentUser, User
from ..deps import DB
from ..errors import Forbidden, NotAuthenticated, NotFound
from ..services import events as event_service
from ..services import results, scoring

router = APIRouter(prefix="/api", tags=["judging"])


def _scores_response(db, judge_id: str, event_id: str | None, event_ids: list[str] | None) -> dict[str, Any]:
    row = db.execute("SELECT id, name FROM users WHERE id = ?", (judge_id,)).fetchone()
    scores = scoring.scores_for_judge(db, judge_id, event_id, event_ids=event_ids)
    return {"judge": {"id": row["id"], "name": row["name"]}, "count": len(scores), "scores": scores}


def _authorize(db, actor: User | None, judge_ref: str | None, event_id: str | None) -> tuple[str, list[str] | None]:
    """Resolve whose scorecards may be read, and from which events.

    A judge reads their own. An organizer reads a judge's cards only from
    events that organizer runs *and* that judge serves; being an organizer
    somewhere never unlocks another event's scores. Admins read everything.
    """
    if actor is None:
        raise NotAuthenticated()
    if judge_ref is None or judge_ref in (actor.id, actor.email):
        if not policy.is_judge_anywhere(db, actor):
            raise Forbidden("Only judges have scorecards. Organizers can pass ?judge=<id>.", code="not_a_judge")
        return actor.id, None
    judge_id = scoring.resolve_judge_id(db, judge_ref)
    if judge_id is None:
        if actor.is_admin:
            raise NotFound("No judge with that id.")
        raise Forbidden("Judges can only read their own scores.", code="peer_scores_forbidden")
    if actor.is_admin:
        return judge_id, None
    shared = policy.events_organized_and_judged(db, actor, judge_id)
    if not shared or (event_id is not None and event_id not in shared):
        raise Forbidden("Judges can only read their own scores.", code="peer_scores_forbidden")
    return judge_id, sorted(shared)


@router.get("/judge/scores", summary="Read your own scorecards (or, for organizers, a judge's)")
def my_scores(db: DB, user: CurrentUser, judge: str | None = Query(default=None, description="judge user id or email"),
              event: str | None = Query(default=None, description="limit to one event id")):
    judge_id, scope = _authorize(db, user, judge, event)
    return _scores_response(db, judge_id, event, scope)


@router.get("/judges/{judge_id}/scores", summary="Read one judge's scorecards")
def judge_scores(db: DB, user: CurrentUser, judge_id: str, event: str | None = None):
    resolved, scope = _authorize(db, user, judge_id, event)
    return _scores_response(db, resolved, event, scope)


@router.get("/events/{event_id}/results", summary="Ranked results (organizers always; everyone after publication)")
def event_results(db: DB, user: CurrentUser, event_id: str, method: str | None = None):
    event = event_service.get_event(db, event_id)
    if policy.is_organizer(db, user, event.id):
        return results.serialize(results.compute_table(db, event, method), include_methods=True)
    return results.serialize(results.visible_table(db, user, event), include_methods=False)
