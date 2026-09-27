# Doggfather

> Build the platform that will judge you.

Doggfather is an open source, self-hostable submission and judging platform
for hackathons, built for **DOGFOOD 2026**. It runs on one laptop with the
network off.

```
docker compose up
```

That starts a seeded portal at <http://localhost:8080> with the official
`fixtures.json` loaded (41 projects, 40 teams, 30 judges, 126 scorecards) and
prints the test logins:

```
seeded. test logins:
  organizer    Cookie: session=org_7f2a
  judge_a      Cookie: session=jdg_a_91bc
  judge_b      Cookie: session=jdg_b_44de
  participant  Cookie: session=prt_2e88
  (web logins: any seeded email, password dogfood-demo-2026)
```

Port 8080 taken? `DOGFOOD_PORT=18080 docker compose up`.

## Acceptance

```
python3 run.py .dogfood.toml > acceptance-report.txt
```

[`acceptance-report.txt`](acceptance-report.txt) is committed as produced:
**claimed T1 T2, verified T1 T2**. It was generated with
[`scripts/offline-acceptance.sh`](scripts/offline-acceptance.sh), which boots
the portal in a container with `--network none` (loopback only) and runs the
unmodified `run.py` against it. That covers both the checker and the
network-off rule. The same check also runs in the test suite
([`tests/test_contract.py`](tests/test_contract.py)) against a live server.

## Demo accounts

Demo mode (`DOGFOOD_DEMO=1`, the compose default) gives every seeded account
the password `dogfood-demo-2026`.

| Role | Email | Notes |
|---|---|---|
| Admin | `admin@doggfather.local` | global admin |
| Organizer | `organizer@doggfather.local` | organizes the fixture event |
| Judge A | `jonas.vogel@example.org` | fixture `jdg_26`, tracks Accessibility and Developer tools |
| Judge B | `diego.herrera@example.org` | fixture `jdg_24`, shares 5 projects with judge A |
| Participant | `priya1@example.org` | team NorthKiln, project Glass Signal |

Set `DOGFOOD_DEMO=0` for a real deployment: no fixed sessions, no shared
passwords. Imported people claim their accounts through "forgot password".

## What works

**T1 core**
- Accounts with scrypt-hashed passwords; server-side sessions stored hashed;
  CSRF protection; rate-limited login; password reset via the local outbox.
- Roles per event (participant, judge, organizer) plus global admin. Visitors
  need no account.
- Events with every phase date, tracks, prizes and custom submission questions.
- Teams by invite link, with a size cap, one team per person per event, and a
  lock at the deadline.
- Submissions: draft, edit, submit. All standard fields plus thumbnail and
  image gallery (type-checked by magic bytes).
- The deadline holds on every write path, HTML and JSON. Late attempts get
  `403 submissions_closed` and are audited.
- A public gallery with search, multi-track and multi-tag filters, and sorting.

**T2 judging**
- Judge invitations by email with a track scope; one-click accept.
- Automatic, track-aware, load-balanced assignment that keeps the judge
  graph connected. Manual batches too.
- Weighted rubric with per-track overrides; re-weighting never touches
  stored scores.
- Isolation in the backend: judges read only their own scores (`403` for peers,
  including unknown ids) and only projects in their queue; aggregates stay
  sealed until published.
- Keyboard-first scoring screen: digits score, Enter saves and opens the next project.
- Live organizer progress dashboard: delinquent judges, abandoned batches,
  per-track coverage.
- Cross-judge normalization with four methods side by side. The default is a
  ridge judge-bias model; the others are shrunk z-score, raw mean, and
  within-judge Bradley–Terry. It is simulation-tested.
- CSV exports for results, scores, projects, teams, judges, assignments and the audit trail.

## Running the tests

```
pip install -r requirements-dev.txt
pytest
```

or inside Docker: `docker compose --profile test run --rm tests`.

## Run without Docker

```
pip install -r requirements.txt
DOGFOOD_DEMO=1 PYTHONPATH=src python -m doggfather serve
```

Settings are environment variables prefixed `DOGFOOD_`; see
[`src/doggfather/config.py`](src/doggfather/config.py).

## License

MIT, see [LICENSE](LICENSE).
