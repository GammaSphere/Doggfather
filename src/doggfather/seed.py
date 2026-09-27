"""Demo accounts and fixture seeding.

``python -m doggfather serve`` calls ``seed()`` on boot. On an empty
database it loads fixtures.json. In demo mode (``DOGFOOD_DEMO=1``, the
docker compose default) it also creates well-known accounts and the fixed
sessions that the acceptance checker sends. Outside demo mode none of those
exist: imported people have no password until they claim their account.

Seeding is idempotent: a restart with a populated volume only refreshes the
demo sessions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from . import audit, auth, clock
from .config import Settings
from .db import fetch_value, transaction
from .security import hash_password
from .services import assignment, bundles, duplicates
from .services.events import get_event

DEMO_PASSWORD = "dogfood-demo-2026"

# role -> (email, display name). Judges and the participant are people from
# fixtures.json; the organizer and admin are platform accounts.
DEMO_PEOPLE = {
    "admin": ("admin@doggfather.local", "Ada Admin"),
    "organizer": ("organizer@doggfather.local", "Olu Organizer"),
    "judge_a": ("jonas.vogel@example.org", "Jonas Vogel"),
    "judge_b": ("diego.herrera@example.org", "Diego Herrera"),
    "participant": ("priya1@example.org", "Priya"),
}

# The exact cookies documented in .dogfood.toml.
DEMO_SESSIONS = {
    "organizer": "org_7f2a",
    "judge_a": "jdg_a_91bc",
    "judge_b": "jdg_b_44de",
    "participant": "prt_2e88",
}

# The fixtures describe a finished hackathon. For the demo we keep its real
# submission cutoff (so late submissions are refused, as the checker
# expects) and open the later phases relative to boot time, so judging and
# voting can be exercised.
JUDGING_WINDOW = timedelta(days=30)


def demo_accounts() -> list[dict[str, str]]:
    return [
        {"role": role, "email": email, "name": name, "password": DEMO_PASSWORD}
        for role, (email, name) in DEMO_PEOPLE.items()
    ]


@dataclass
class SeedResult:
    seeded: bool
    event_id: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def find_fixtures(settings: Settings) -> Path | None:
    candidates = [settings.fixtures_path] if settings.fixtures_path else []
    candidates += [Path("fixtures.json"), Path(__file__).resolve().parents[2] / "fixtures.json"]
    for path in candidates:
        if path and Path(path).is_file():
            return Path(path)
    return None


def is_seeded(db: sqlite3.Connection) -> bool:
    return fetch_value(db, "SELECT value FROM meta WHERE key = 'seeded_at'") is not None


def seed(db: sqlite3.Connection, settings: Settings, fixtures: dict | None = None) -> SeedResult:
    if is_seeded(db):
        if settings.demo:
            ensure_demo_sessions(db, settings)
        return SeedResult(seeded=False)

    if fixtures is None:
        path = find_fixtures(settings)
        if path is None:
            return SeedResult(seeded=False)
        fixtures = json.loads(path.read_text(encoding="utf-8"))

    with transaction(db):
        admin_email, admin_name = DEMO_PEOPLE["admin"]
        org_email, org_name = DEMO_PEOPLE["organizer"]
        admin = auth.ensure_user(db, admin_email, admin_name, verified=True)
        db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin.id,))
        organizer = auth.ensure_user(db, org_email, org_name, verified=True)

        report = bundles.import_bundle(
            db, fixtures, actor=organizer,
            overrides={"judging_close": clock.iso(clock.now() + JUDGING_WINDOW),
                       "tagline": fixtures["event"].get("tagline") or "The shared DOGFOOD fixture event",
                       # Email-gated quadratic vote, open for the same window as judging.
                       "voting_mode": fixtures["event"].get("voting_mode", "email"),
                       "voting_open": clock.iso(clock.now() - timedelta(hours=1)),
                       "voting_close": clock.iso(clock.now() + JUDGING_WINDOW)},
        )

        # The fixtures carry unfinished review batches (8 projects with only two
        # reviews). Top every project up to the review target with *pending*
        # assignments so the progress dashboard shows real outstanding work.
        topup = assignment.run_auto(db, None, get_event(db, report.event_id))
        # The fixtures carry one duplicate submission (prj_41 repeats prj_07).
        duplicates.refresh(db, report.event_id)
        report.counts["pending_assignments"] = len(topup.created)

        if settings.demo:
            # One hash for every seeded account keeps boot fast; demo only.
            demo_hash = hash_password(DEMO_PASSWORD)
            db.execute("UPDATE users SET password_hash = ?, email_verified_at = COALESCE(email_verified_at, ?)",
                       (demo_hash, clock.now_iso()))
            for role, (email, name) in DEMO_PEOPLE.items():
                db.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))

        counts = dict(report.counts)
        audit.record(db, "seed.fixtures", event_id=report.event_id, detail={
            "projects": counts.get("projects", 0), "judges": counts.get("judges", 0), "scores": counts.get("scores", 0),
        })
        db.execute("INSERT INTO meta (key, value) VALUES ('seeded_at', ?)", (clock.now_iso(),))

    if settings.demo:
        ensure_demo_sessions(db, settings)
    return SeedResult(seeded=True, event_id=report.event_id, counts=counts, warnings=report.warnings)


def ensure_demo_sessions(db: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    """(Re)create the fixed checker sessions. Demo mode only."""
    assert settings.demo, "demo sessions must never exist outside demo mode"
    created = {}
    for role, token in DEMO_SESSIONS.items():
        email = DEMO_PEOPLE[role][0]
        user = auth.get_user_by_email(db, email)
        if user is None:
            continue
        auth.create_session(db, settings, user.id, "seed", "doggfather-seed", token=token, lifetime=timedelta(days=365))
        created[role] = token
    return created


def banner(settings: Settings, result: SeedResult) -> str:
    lines = []
    if result.seeded:
        c = result.counts
        lines.append(f"loaded fixtures: {c.get('projects', 0)} projects, {c.get('teams', 0)} teams, "
                     f"{c.get('judges', 0)} judges, {c.get('scores', 0)} scores")
    if settings.demo:
        lines.append("seeded. test logins:")
        for role, token in DEMO_SESSIONS.items():
            lines.append(f"  {role:<12} Cookie: session={token}")
        lines.append(f"  (web logins: any seeded email, password {DEMO_PASSWORD})")
    else:
        lines.append("ready. demo accounts are disabled (DOGFOOD_DEMO=0)")
    lines.append(f"portal: {settings.base_url}")
    return "\n".join(lines)
