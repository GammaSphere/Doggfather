"""Domain errors.

Services raise these and never build HTTP responses. The web layer maps each
error to a JSON body (API clients, curl, the acceptance checker) or to a
rendered page (browsers), keeping the same status code in both cases.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400
    code = "bad_request"
    default_message = "The request could not be processed."

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 status: int | None = None, **detail: Any) -> None:
        self.message = message or self.default_message
        if code:
            self.code = code
        if status:
            self.status = status
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.detail}


class NotAuthenticated(AppError):
    status = 401
    code = "not_authenticated"
    default_message = "Log in to continue."


class Forbidden(AppError):
    status = 403
    code = "forbidden"
    default_message = "You do not have access to this."


class SubmissionsClosed(Forbidden):
    code = "submissions_closed"
    default_message = "Submissions for this event are closed."


class ResultsHidden(Forbidden):
    code = "results_hidden"
    default_message = "Results stay sealed until the organizers publish them."


class NotFound(AppError):
    status = 404
    code = "not_found"
    default_message = "Nothing here."


class Conflict(AppError):
    status = 409
    code = "conflict"
    default_message = "That conflicts with the current state."


class ValidationFailed(AppError):
    status = 422
    code = "validation_failed"
    default_message = "Some fields need attention."

    def __init__(self, message: str | None = None, *, fields: dict[str, str] | None = None, **detail: Any) -> None:
        super().__init__(message, fields=fields or {}, **detail)
        self.fields = fields or {}


class RateLimited(AppError):
    status = 429
    code = "rate_limited"
    default_message = "Too many requests. Slow down and try again shortly."

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after
