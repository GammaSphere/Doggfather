"""The single source of "now".

Deadlines are the heart of T1, so time is injectable: tests freeze the clock
on either side of a cutoff instead of sleeping or editing fixture dates.
All timestamps are stored as ISO 8601 UTC strings with second precision
(``2026-03-01T18:00:00Z``), which also makes them sort correctly as text.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_frozen: datetime | None = None


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    return datetime.now(UTC).replace(microsecond=0)


def now_iso() -> str:
    return iso(now())


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    """Parse ISO 8601; naive values are taken to be UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def normalize(value: str) -> str:
    """Round-trip any ISO 8601 string into the canonical storage form."""
    return iso(parse(value))


def in_(delta: timedelta) -> str:
    return iso(now() + delta)


def set_now(value: datetime | None) -> None:
    global _frozen
    _frozen = value.astimezone(UTC).replace(microsecond=0) if value else None


@contextmanager
def frozen(value: datetime | str):
    """Freeze time for the duration of a block (tests and seeding)."""
    previous = _frozen
    set_now(parse(value) if isinstance(value, str) else value)
    try:
        yield
    finally:
        set_now(previous)
