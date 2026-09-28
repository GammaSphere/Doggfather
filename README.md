# Doggfather

> Build the platform that will judge you.

Doggfather is an open source, self-hostable **hackathon submission and
judging platform**, built for DOGFOOD 2026. It runs an event end to end
(registration, teams, submissions, a public gallery, judging, normalization,
community voting, results, certificates, archive) on one laptop with the
network off.

```
docker compose up
```

That serves a seeded portal at **http://localhost:8080** with the official
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

---

## Acceptance

[`acceptance-report.txt`](acceptance-report.txt) is committed exactly as
`run.py` printed it: **all seven checks pass.**

We claim **T1, T2, T3 and T4**, because all four are built and tested. The
official checker only has checks for T1 and T2, so its report reads
"verified T1 T2" and adds "claimed but not verified: T3 T4": there is
nothing in `run.py` that *could* verify them, for any team. T3 and T4 are
verified by our own suite instead, and every feature in the tables below
names its test. If you prefer to read the claim as T1–T2 only, change one line in
`.dogfood.toml` and rerun the checker.

It was produced by [`scripts/offline-acceptance.sh`](scripts/offline-acceptance.sh),
which boots the built image in a container started with `--network none`
(loopback only) and runs the **unmodified** `run.py` against it with this
repository's `.dogfood.toml`. That demonstrates the checks and the network-off
rule together. The same checker also runs inside the test suite against a
live server ([`tests/test_contract.py`](tests/test_contract.py)).

```
python3 run.py .dogfood.toml > acceptance-report.txt      # with the portal up
sh scripts/offline-acceptance.sh > acceptance-report.txt   # or fully offline
```

The official checker only has T1 and T2 checks. T3 and T4 are verified by
our own suite; each feature below names the test that proves it.

## Demo accounts

With `DOGFOOD_DEMO=1` (the compose default), every seeded account uses the
password `dogfood-demo-2026`.

| Role | Email | Notes |
|---|---|---|
| Admin | `admin@doggfather.local` | outbox, people, global audit |
| Organizer | `organizer@doggfather.local` | runs the fixture event |
| Judge A | `jonas.vogel@example.org` | fixture `jdg_26`: Accessibility + Developer tools; has a pending review |
| Judge B | `diego.herrera@example.org` | fixture `jdg_24`: shares 5 projects with judge A |
| Participant | `priya1@example.org` | team NorthKiln, project Glass Signal |

**For a real deployment set `DOGFOOD_DEMO=0`.** That removes the fixed sessions
and shared passwords; imported people claim their accounts through "forgot
password" (the mail lands in the outbox, or in real inboxes with SMTP set).

## What it does

### T1: core
| Feature | Where | Proof |
|---|---|---|
| Accounts, server-side sessions (hashed), CSRF, rate-limited login, password reset | `auth.py`, `csrf.py` | `test_auth.py` |
| Roles: visitor, participant, judge, organizer (per event), admin (global) | `policy.py` | `test_isolation.py` |
| Events with every phase date, tracks, prizes, custom questions, phase controls | `services/events.py` | `test_events.py` |
| Teams by invite link: size cap, one team per event, captain tools, lock at deadline | `services/teams.py` | `test_teams.py` |
| Draft → edit → submit, all standard fields, thumbnail and image gallery | `services/projects.py` | `test_submissions.py` |
| **Deadline enforced server-side on every write path**, refusals audited | `assert_submissions_open` | `test_deadline_is_enforced_to_the_second` |
| Public gallery: search, multi-track (OR) and multi-tag (AND) filters | `services/gallery.py` | `test_gallery.py` |

### T2: judging
| Feature | Where | Proof |
|---|---|---|
| Judge invitations by email with track scope | `services/judges.py` | `test_judging_setup.py` |
| Assignment: manual batches, and a track-aware, load-balanced, overlap-diverse automatic assigner | `services/assignment.py` | `test_judging_setup.py` |
| Weighted rubric with per-track overrides; scale frozen once scoring starts | `services/rubric.py` | `test_judging_setup.py` |
| **Backend isolation**: own scores only; peers and unknown ids get 403; track-scoped queues | `api/judging.py`, `policy.py` | `test_isolation.py` |
| Keyboard-first scoring screen (digits, then Enter for the next project) | `web/judge.py`, `score.js` | `test_isolation.py` |
| Live progress dashboard: delinquent judges, abandoned batches, coverage | `services/progress.py` | `test_exports.py` |
| **Documented cross-judge normalization**: four methods side by side, simulation-tested | `services/normalization.py`, [JUDGING.md](JUDGING.md) | `test_normalization.py` |
| CSV export at every stage (7 kinds, formula-safe) | `services/exports.py` | `test_exports.py` |

### T3: public
| Feature | Where | Proof |
|---|---|---|
| Community voting with a choice of gate: account, verified email (one-time code) or open link | `services/voting.py` | `test_voting.py` |
| **Quadratic voting** (n votes cost n² credits), enforced by triggers | migration `002` | `test_database_trigger_enforces_the_budget` |
| Randomized, per-voter stable ballot order | `ballot_order` | `test_each_voter_gets_a_stable_personal_shuffle` |
| Results and tallies hidden until voting closes and results are published | `results.py`, `voting.py` | `test_results_are_sealed_until_published` |
| Comments with moderation | `services/comments.py` | `test_integrity.py` |
| Anti-abuse: rate limits, duplicate-submission detection, network flags, voids, hash-chained audit trail in plain English | `duplicates.py`, `audit.py` | `test_integrity.py`, `test_auth.py` |

### T4: stretch
| Feature | Where | Proof |
|---|---|---|
| REST API v1 (~70 endpoints) with personal tokens; offline reference at `/api/docs` | `api/v1.py` | `test_api.py` |
| Webhooks: transactional outbox, HMAC-signed, retries with backoff, delivery log | `services/webhooks.py` | `test_webhooks.py` |
| Certificates (participation, awards), printable | `services/records.py` | `test_records.py` |
| **Signed, publicly verifiable judge records** (Ed25519 plus a score commitment), offline verifier | `signing.py`, `tools/verify_record.py` | `test_records.py` |
| Embeddable gallery widget (`embed.js` or iframe) | `web/embed.py` | `test_bundles.py` |
| Bulk import (admins) and export (bundle format, lossless round trip; also CLI) | `services/bundles.py` | `test_bundles.py` |

### Bonus challenges
- **Normalization proof**: [JUDGING.md](JUDGING.md) plus the generated
  [docs/normalization-report.md](docs/normalization-report.md). With judge
  bias planted, the default method recovers the true ranking better than raw
  means in 40 of 40 simulated events.
- **Pairwise mode**: judges compare projects side by side; pairs are chosen
  actively and ranked by Bradley–Terry (MM algorithm). `test_pairwise.py`
- **Threat model**: [THREAT-MODEL.md](THREAT-MODEL.md), mitigated and
  unmitigated risks listed honestly.
- **API first**: [docs/openapi.json](docs/openapi.json) (OpenAPI 3.1); the
  test suite fails if it drifts from the running app.

## Five-minute demo

[Demo.md](Demo.md) is the full recording script (create, submit, judge,
publish) with setup, clicks, narration and timings. Every step in it is
executed by `tests/test_demo_walkthrough.py`, so the script cannot go stale.

## Documentation

| Document | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request lifecycle, security design, configuration, trade-offs |
| [DATA-MODEL.md](DATA-MODEL.md) | Schema, invariants in the database, the bundle format, import and export paths, backups |
| [JUDGING.md](JUDGING.md) | Assignment, scoring maths, normalization and its defense, isolation, pairwise, voting |
| [THREAT-MODEL.md](THREAT-MODEL.md) | Sybil, stuffing, collusion, cutoff gaming, IDOR, CSRF, XSS, SSRF…; mitigated vs not |
| [docs/normalization-report.md](docs/normalization-report.md) | Generated from the fixtures: every project, every method, judge calibration, λ sensitivity, simulation |
| [Demo.md](Demo.md) | The five-minute demo script, click by click |
| [docs/PLAN.md](docs/PLAN.md) | The build plan and requirement traceability we started from |
| `/api/docs` in the running portal | REST API reference |

## Running without Docker

```
pip install -r requirements.txt
DOGFOOD_DEMO=1 PYTHONPATH=src python -m doggfather serve
```

Other commands: `python -m doggfather migrate | seed | import <bundle> | export <event_id> [file] | openapi`,
`python -m doggfather.tools.normalization_report fixtures.json`, and
`python -m doggfather.tools.verify_record record.json key.pem`.
Configuration is environment variables prefixed `DOGFOOD_` (table in
[ARCHITECTURE.md](ARCHITECTURE.md#configuration)).

## Tests

```
pip install -r requirements-dev.txt
pytest                                      # 210 tests, ~30 s
docker compose --profile test run --rm tests
```

## Known limitations

Written down so nobody has to discover them:

- **One process.** Rate-limit state is in memory and the webhook worker is a
  thread. Scaling out needs a shared limiter and a single worker.
- **SQLite has a single writer.** Plenty for events of this size; Postgres
  would mean porting six migration files.
- **Normalization corrects each judge's location (harsh or generous), not
  their spread.** The z-score and Bradley–Terry views are there for
  comparison. Collusion by several judges in one track is not detectable by
  maths alone.
- **Link-gated voting is Sybil-weak by nature**, even with network flags and
  the quadratic cap. Use the email or account gate when stakes are real.
- **Pairwise mode is plain Bradley–Terry**, without Crowd-BT's per-judge
  reliability weights.
- **Mail is delivered to the local outbox** unless SMTP is configured.
- **HTTP only out of the box.** Put TLS in front and set
  `DOGFOOD_COOKIE_SECURE=1`.
- **The demo video** is recorded by the team following [Demo.md](Demo.md).
- No web fonts are shipped (offline rule). The UI uses Archivo Black and
  JetBrains Mono when installed, and system fonts otherwise.

## Layout

```
src/doggfather/    application (services/, web/, api/, templates/, static/, migrations/, tools/)
tests/             200+ tests, including the official checker end to end
docs/              plan, OpenAPI document, normalization report
scripts/           offline acceptance run
run.py, fixtures.json, .dogfood.toml, acceptance-report.txt
```

## License

MIT, see [LICENSE](LICENSE). The DOGFOOD brief, `run.py` and
`fixtures.json` are by Hackathon Raptors and are included so the checker
runs out of the box.
