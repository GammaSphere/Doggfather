# Doggfather: build plan

Doggfather is our DOGFOOD 2026 entry: an open source, self-hostable hackathon
submission and judging platform. This document is the plan we agreed on before
writing any project code. It maps every requirement in the brief (`spec.md`,
`context.md`, `full-spec.md`, `run.py`, `fixtures.json`) to the component that
satisfies it and the test that proves it.

---

## 1. Goals, in priority order

1. **T1 and T2 pass the official checker (`run.py`) cleanly.** Correctness beats breadth.
2. **Judging integrity**: isolation enforced in the backend, a normalization method
   we can defend with maths and a simulation, and an audit trail an organizer can read.
3. **Adoptability**: `docker compose up` gives a seeded portal on `localhost:8080`
   with the network off. The docs are written so a stranger can run it.
4. **T3, then T4, then the bonus challenges.** We claim a tier only when every item
   in it works and has tests.

## 2. Stack and why

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | We know it well, and the normalization and Bradley-Terry maths are easy to read in it. |
| Web | FastAPI + Jinja2 (server-rendered HTML) | One process serves the HTML UI and the JSON API. OpenAPI comes for free (API First bonus). Guards are dependency-injected, so an authz check cannot be skipped by accident. |
| Storage | SQLite (stdlib `sqlite3`, WAL), explicit SQL migrations | One file. Nothing to host. Backup is a file copy. The schema is plain SQL we can defend line by line. |
| Crypto | stdlib `hashlib.scrypt` for passwords, `cryptography` Ed25519 for signed records | Signatures anyone can verify with the published public key. |
| Frontend | Vanilla JS, progressive enhancement, self-hosted fonts | No build step, no CDN, works offline. |
| Tests | pytest + FastAPI TestClient | Also runs inside the container. |
| Packaging | Multi-stage Dockerfile, non-root user, healthcheck, `/data` volume | One command to running. |

Runtime makes **zero** outbound network calls. Mail goes to a local outbox that
admins can browse; SMTP is optional through environment variables. Webhook
delivery fails gracefully when there is no network.

## 3. Architecture

```
src/doggfather/
  app.py            create_app(): middleware (sessions, CSRF, security headers), routers
  config.py         settings from env (DATA_DIR, DEMO mode, SECRET, SMTP, ...)
  clock.py          injectable UTC clock, so tests can freeze time around deadlines
  db.py             connections, transactions, migration runner
  migrations/       001_init.sql, 002_..., applied in order and recorded in schema_migrations
  security.py       scrypt hashing, token generation, CSRF, constant-time compare
  auth.py           session lifecycle, current_user, require_* guards
  policy.py         the single authorization matrix (who can see what), used by UI and API alike
  audit.py          hash-chained audit log plus human-readable rendering
  ratelimit.py      token-bucket limiter (login, register, votes, comments, codes)
  services/         business logic, no HTTP in here
    events.py teams.py projects.py uploads.py assignment.py scoring.py
    normalization.py pairwise.py voting.py comments.py duplicates.py
    exports.py bundles.py webhooks.py signing.py certificates.py mailer.py
  web/              HTML routes (public, auth, participant, judge, organizer, admin, vote, embed)
  api/              JSON REST API v1 (bearer tokens or session), plus the checker contract routes
  templates/        Jinja2, with the dogfoodhack.com design language
  static/           css, js, self-hosted fonts (OFL), images
  seed.py           loads fixtures.json, creates demo accounts, prints test logins
  tools/            normalization_report.py, verify_record.py
tests/              unit and integration suites
```

Layering rule: routes → policy → services → db. Every judge-scoped query in
`services/scoring.py` takes a `judge_id` argument and filters on it in SQL, so
isolation does not depend on a template remembering to hide something.

## 4. Data model (summary; DATA-MODEL.md has the full version)

Ids are opaque strings. Ids from the fixtures are kept verbatim (`evt_01`,
`prj_07`, `jdg_26`) so an export can be re-imported and diffed. Timestamps are
ISO 8601 UTC.

- `users` (email unique, case-insensitive; `is_admin`); `sessions` (stores only the SHA-256 of the token)
- `events` (submission open/close, judging close, voting window, voting mode, QV credits, results_published_at, normalization method)
- `event_members(event_id, user_id, role)` with role one of organizer, judge, participant. Roles are per event, so someone can judge one event and compete in another. Admin is global.
- `tracks`, `prizes`, `custom_questions`, `judge_tracks`
- `teams` (names are **not** unique; the fixtures repeat StillTrail three times), `team_members` (one team per user per event, enforced by a unique index), invite codes
- `projects` plus `project_tags`, `project_images`, `project_answers`. Status is draft, submitted or withdrawn. `duplicate_of` holds duplicate-detection flags.
- `criteria` (weights), `criterion_track_weights` (per-track overrides)
- `assignments` (judge × project, batch label, pending/done/skipped), `scores` + `score_items`
- `pairwise_comparisons`
- `voters`, `ballot_items` (quadratic voting), `comments`
- `audit_log` (hash chain), `invites`, `outbox`, `api_tokens`, `webhooks`, `webhook_deliveries`, `records` (signed certificates and judge records)

## 5. Requirement traceability

### Required deliverables
| Requirement | Where | Proof |
|---|---|---|
| `docker compose up` gives a seeded portal, offline at runtime | Dockerfile, docker-compose.yml, seed.py | Manual boot, then `run.py` |
| OSI license | `LICENSE` (MIT) | — |
| `.dogfood.toml` at root, honest claims | repo root | `run.py` |
| `acceptance-report.txt` committed | repo root | generated by `run.py` against the container |
| README / ARCHITECTURE / DATA-MODEL / JUDGING | repo root | — |
| `tests/` beyond the checker | `tests/` | `pytest` |
| Seed prints test logins | `seed.py` on boot | container log |
| Demo video | README includes a 5-minute demo script | recorded by the team |

### T1 Core
| Requirement | Implementation |
|---|---|
| Auth and sessions | Email + password (scrypt). Server-side sessions, HttpOnly SameSite=Lax cookie, rotated on login, sliding expiry, logout and "log out everywhere", rate-limited login, password reset through the outbox. |
| Roles: visitor, participant, judge, organizer, admin | Visitor means no session. The other roles are per event (`event_members`); admin is global. All checks live in `policy.py`. |
| Event creation: dates, tracks, prizes | Organizer event form: every phase date, tracks, prizes (overall or per track), custom questions, team size, voting settings. |
| Team formation by invite link | Create a team to get `/join/<code>`. Regenerate the code, remove members, leave, enforce team size, one team per event. Team changes lock at the deadline. |
| Draft and edit until the deadline | Draft, then submit, then keep editing until close. Required fields are validated on submit. |
| Submission fields | Name, tagline, long description, thumbnail, image gallery, demo video URL, repo URL, live URL, tech tags, track, custom answers. Uploads are checked by magic bytes and size, and stored under random names. |
| Deadline enforcement | One `assert_submissions_open(event)` guard on every write path (create, edit, submit, upload, delete, team change), HTML and API alike. Returns 403 with `submissions_closed`. |
| Public gallery, search, multi-filter | `/projects`: full-text search across title, tagline, description, tags and team; multiple track and tag filters; sort; project pages. |

### T2 Judging
| Requirement | Implementation |
|---|---|
| Judge invitation | The organizer invites by email and track. The invite link sits in the outbox; accepting it grants the judge role. |
| Assignment, batch or automatic | Manual batch (judge × chosen projects) or automatic: a track-aware, least-loaded greedy that targets k reviews per project, skips conflicts of interest, and spreads judge overlap so the normalization graph stays connected. Each run gets a batch label. |
| Weighted rubric per event and track | Rubric editor: criteria, descriptions, weights, per-track weight overrides. Weighted score = Σw·s / Σw. |
| Backend isolation | `policy.py` matrix: judges get only their own scores, get 403 for peers, get 403 on projects outside their tracks, and get 403 on aggregates until results are published. Tested per role against the actual endpoints. |
| Live progress dashboard | Per-judge completion, last activity, flags for delinquent, abandoned-batch and flat-scorer judges, per-track coverage, per-project review counts. Polls a JSON endpoint. |
| Normalization, documented | Four methods side by side: raw, shrunk z-score, additive judge-bias model (ridge, the default), and within-judge Bradley-Terry. Rank-movement table. JUDGING.md defends the choice. |
| CSV export at every stage | projects, teams, judges, assignments, scores, results, votes, audit. Cells are escaped against formula injection. |

### T3 Public
| Requirement | Implementation |
|---|---|
| Community voting: link, email or authenticated | The event's voting mode picks the gate. Email mode sends a one-time code to the outbox. Link mode binds a signed device token. |
| Quadratic voting | Credit budget C. Casting v votes costs v² credits, enforced inside a server transaction. |
| Comments | Logged-in users only. Length-limited and rate-limited. Organizers can hide them, and that is audited. |
| Results hidden during the window | Public tallies return `results_hidden` until voting closes and results are published. Organizers see live counts. |
| Randomized ballot order | Each voter gets a stable shuffle seeded with HMAC(secret, voter, event). |
| Anti-abuse | Rate limits, duplicate-submission detection (normalized title, repo URL, text similarity; flags prj_41), ballot anomaly flags (IP and device bursts), and a human-readable, hash-chained audit trail with a verify button. |

### T4 Stretch
| Requirement | Implementation |
|---|---|
| REST API + webhooks | `/api/v1/*` with session or bearer-token auth; OpenAPI 3 at `/api/openapi.json` and an offline docs page. Webhooks are signed with HMAC-SHA256 and retried with backoff; the delivery log is visible. |
| Certificates | Participation, award and judge certificates as printable pages, each signed and linked to a verification page. |
| Signed, verifiable judge records | An Ed25519 signature over canonical JSON: judge, event, review count, and a SHA-256 commitment to the judge's score set. The public key is at `/.well-known/doggfather/signing-key.pem`, alongside `/verify` and an offline verifier script. |
| Embeddable gallery | `/embed/events/<slug>` (the only frameable route) plus `/embed.js`, which auto-resizes. |
| Bulk import and export | A JSON bundle (superset of the fixtures shape) for export and import, from the UI and the CLI, with a round-trip test. |

### Fixture edge cases
| Case in fixtures | How we handle it |
|---|---|
| Flat scorers jdg_07 (4/4/4 ×3) and jdg_01 (a single 2/2/2) | Variance shrinkage means a flat judge carries no ranking signal instead of dividing by zero. Flagged on the dashboard. |
| Unfinished batches (8 projects with only 2 reviews) | Scores are imported faithfully. A documented seed step tops coverage up to 3 with *pending* assignments, so the dashboard shows real outstanding work. |
| Duplicate submission prj_41 ≈ prj_07 | Duplicate detector flags it (same team, title and repo). The organizer resolves it. |
| Repeated team names | No uniqueness constraint on team name. |
| 2 to 5 reviews per project | Every aggregate is count-aware, and the bias model weighs projects by review count. |

### Bonus challenges
- **Normalization proof**: JUDGING.md (maths), `docs/normalization-report.md` (raw vs normalized vs rank movement on the fixtures, generated by a tool), and a simulation test that plants judge bias and shows normalized ranks recover the truth better than raw ones (Spearman ρ).
- **Pairwise mode**: pairwise judging UI with Bradley-Terry fitted by MM (Hunter 2004), with a prior for sparse or undefeated items and active pair selection.
- **Threat model**: THREAT-MODEL.md covering Sybil voting, ballot stuffing, scraping, collusion, cutoff gaming, IDOR, CSRF, XSS, CSV injection and webhook SSRF, each marked mitigated or not.
- **API First**: OpenAPI spec committed and checked for drift in tests.

## 6. Checker contract (`.dogfood.toml`)

```toml
[portal]
base_url = "http://localhost:8080"
[auth]    # demo sessions exist only when DOGFOOD_DEMO=1 (the compose default)
organizer   = "Cookie: session=org_7f2a"
judge_a     = "Cookie: session=jdg_a_91bc"   # jdg_26 Jonas Vogel
judge_b     = "Cookie: session=jdg_b_44de"   # jdg_24 Diego Herrera (shares 5 projects with jdg_26)
participant = "Cookie: session=prt_2e88"     # priya1@example.org, team NorthKiln
[routes]
gallery      = "/projects"
submit       = "/projects/new"
judge_scores = "/api/judge/scores"
peer_scores  = "/api/judges/jdg_26/scores"
csv_export   = "/api/events/evt_01/export/results.csv"
```

Tier claims will list only tiers that are complete and tested. The checker only
exercises T1 and T2, so the README explains how T3 and T4 are verified, with our
own tests.

## 7. UI design language (after dogfoodhack.com)

- Background `#0B1020` with a `#0E1428` panel. Text `#E6ECFF`, secondary `#AEBAD6`, muted `#6B7A9E`.
- Accents: pink `#FF3D6E` for primary actions and numbers, teal `#00E5D0` for rules, labels and focus.
- Square 1px borders in `#1B2540`, hover `#26355C`. No rounded corners.
- Type: Archivo Black for display (uppercase, tight tracking), JetBrains Mono for body, Bebas Neue and VT323 for bracket labels (`[ 03 / JUDGING ]`, `[ LOCKED ]`), Playfair Display italic for lead paragraphs. All self-hosted under the OFL.
- Signature pieces: a marquee ticker, a ghost outlined section number with a teal bracket label and rule, a glitch wordmark, a striped progress bar, pink `→` bullets, a grid backdrop with a scanline overlay. `prefers-reduced-motion` is respected.
- Layout works down to phone width. Keyboard-first scoring (1–5, arrows, Enter to save and move on) cuts judge fatigue.

## 8. Delivery plan

Each checkpoint is one commit. It ends with the full `pytest` suite green. It
depends only on the checkpoints above it.

| # | Checkpoint | Main files | Verified by |
|---|---|---|---|
| 1 | Bootstrap: license, packaging, checker and fixtures | `LICENSE`, `pyproject.toml`, `requirements*.txt` | — |
| 2 | App skeleton: config, clock, db and migration runner, health, Docker | `config.py`, `clock.py`, `db.py`, `app.py`, `Dockerfile`, `docker-compose.yml` | `test_health.py` |
| 3 | Schema for T1 and T2 | `migrations/001_init.sql` | `test_migrations.py` |
| 4 | Design system and base layout | `templates/base.html`, `static/css/app.css` | `test_pages.py` |
| 5 | Auth, sessions, CSRF, role policy | `security.py`, `auth.py`, `policy.py`, `web/auth.py` | `test_auth.py` |
| 6 | Fixture import and seed with demo logins, audit log | `services/bundles.py`, `seed.py`, `audit.py` | `test_seed.py` |
| 7 | Events: dates, tracks, prizes, questions | `services/events.py`, `web/organizer.py` | `test_events.py` |
| 8 | Teams by invite link | `services/teams.py`, `web/participant.py` | `test_teams.py` |
| 9 | Submissions and deadline enforcement | `services/projects.py`, `services/uploads.py` | `test_submissions.py` |
| 10 | Public gallery | `web/public.py` | `test_gallery.py` |
| 11 | Rubric, judge invites, assignment engine | `services/scoring.py`, `services/assignment.py`, `services/mailer.py` | `test_assignment.py` |
| 12 | Judge scoring UI and isolation API | `web/judge.py`, `api/contract.py` | `test_isolation.py` |
| 13 | Normalization and results | `services/normalization.py`, `services/pairwise.py` (BT core) | `test_normalization.py` |
| 14 | Progress dashboard, CSV export, `.dogfood.toml` | `services/progress.py`, `services/exports.py` | `test_exports.py`, `test_contract.py` |
| 15 | Acceptance report | `acceptance-report.txt` | `run.py` |
| 16 | Quadratic voting | `services/voting.py`, `web/vote.py` | `test_voting.py` |
| 17 | Comments, rate limits, duplicate detection, audit UI | `services/comments.py`, `services/duplicates.py` | `test_integrity.py` |
| 18 | REST API v1, tokens, OpenAPI, webhooks | `api/v1.py`, `services/webhooks.py` | `test_api.py`, `test_webhooks.py` |
| 19 | Signed records and certificates | `services/signing.py`, `services/records.py` | `test_records.py` |
| 20 | Embed widget and bulk bundles | `web/embed.py`, `services/bundles.py` | `test_bundles.py` |
| 21 | Pairwise judging mode | `web/judge.py`, `services/pairwise.py` | `test_pairwise.py` |
| 22 | Documentation | `ARCHITECTURE.md`, `DATA-MODEL.md`, `JUDGING.md`, `THREAT-MODEL.md` | — |
| 23 | Final acceptance run and tier claims | `.dogfood.toml`, `acceptance-report.txt`, `README.md` | `run.py` against the container |

Commits are published 20 to 30 minutes apart.

## 9. Known limits we will state honestly

- The rate limiter lives in memory, so it assumes one process (the default). A multi-instance setup would need a shared store.
- SQLite has a single writer. That is plenty for ~12 events a year of this size; switching to Postgres would mean porting the migrations.
- Email goes to the local outbox unless SMTP is configured.
- The demo video is recorded by the team; the README carries the script.
