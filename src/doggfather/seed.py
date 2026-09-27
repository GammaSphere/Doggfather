"""Demo accounts and fixture seeding.

In demo mode (``DOGFOOD_DEMO=1``, the docker compose default) the portal
boots with well-known accounts and the fixed sessions that the acceptance
checker sends. Outside demo mode none of these exist.
"""

from __future__ import annotations

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


def demo_accounts() -> list[dict[str, str]]:
    return [
        {"role": role, "email": email, "name": name, "password": DEMO_PASSWORD}
        for role, (email, name) in DEMO_PEOPLE.items()
    ]
