# Threat model

What we defend, from whom, how, and where we knowingly stop. Each threat is
marked **mitigated**, **partial**, or **unmitigated**, and most mitigations
point at the test that exercises them.

## Assets

1. **Result integrity**: the ranking, prizes, and the community tally.
2. **Judge confidentiality**: who scored what, before and after publication.
3. **Submission integrity**: content and timing relative to the deadline.
4. **Accounts**: sessions, API tokens, passwords.
5. **Personal data**: emails, voter identities, IPs (stored only as keyed hashes).
6. **Trust in the record**: the audit trail, certificates, judge records.

## Actors

| Actor | Capability | Goal |
|---|---|---|
| Visitor | Anonymous HTTP, many IPs, scripts | Scrape, stuff votes, probe |
| Participant | Account, a team, a project | Win: late edits, self-votes, peeking at scores |
| Judge | Account, a queue | Read peers' scores, favour friends, collude |
| Organizer | Everything in their event | Rig results quietly |
| Network attacker | Same Wi-Fi as users | Steal sessions |
| Host with database access | Raw SQLite | Rewrite history |

## Threats

### Voting

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| **Sybil voting** (many fake identities) | partial | The gate is the organizer's choice: *account* (one ballot per account; registration rate-limited to 10/hour/IP), *email* (one per verified address; plus-tags and Gmail dots normalized so `a.b+1@gmail.com` = `ab@gmail.com`; codes rate-limited per address and IP), *link* (one per device cookie). Quadratic cost caps any single identity's influence at √C votes per project. Networks with ≥5 voters are auto-flagged; organizers can void, and the tally updates. `test_voting.py` | Code guesses are capped at 5 per code (the counter is committed even when the guess fails). Throwaway email domains and fresh devices on many IPs are still possible. The honest gate for high stakes is *account* plus manual review. Link mode is documented as the weakest. |
| **Ballot stuffing** (one identity, many ballots) | mitigated | Unique voter per account, email or device per event (schema constraints). A ballot *replaces* the previous one. Σv² ≤ credits is checked in a write transaction and by triggers. `test_database_trigger_enforces_the_budget` | Clearing cookies in link mode creates a new device, which the network flag and the quadratic cap bound. |
| **Voting for yourself** | mitigated | Own-team projects are shown as "your team" and refused server-side (account and email identities). `test_participants_cannot_vote_for_their_own_team` | In link mode we do not know who you are. |
| **Watching the tally to coordinate** | mitigated | Tallies return `403 results_hidden` to everyone but organizers until voting closes *and* results are published. `test_tally_is_hidden_until_voting_closes_and_results_publish` | Organizers see it live, by design. |
| **Position bias** | mitigated | Per-voter stable HMAC shuffle. `test_each_voter_gets_a_stable_personal_shuffle` | – |

### Judging

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| **Peer-score access (IDOR)** | mitigated | Backend policy; judge queries carry `judge_id` in SQL. Peer and unknown ids return 403 (no enumeration oracle). Also true for the API and bearer tokens. `test_isolation.py`, `test_api.py`, official checker | – |
| **Cross-event reach by an organizer** (read a shared judge's scores from another event, re-weight another event's rubric, seat people via import) | mitigated | Found in our own review: scorecards are scoped to events the organizer runs *and* the judge serves; rubric overrides must use the event's own criteria and tracks (service check plus triggers, migration 007); bundle import is admin-only. `test_hardening.py` | – |
| **Scoring outside your remit** | mitigated | Scoring requires an assignment in the judge's tracks and an open judging window. `test_judge_cannot_open_projects_outside_their_queue` | – |
| **Conflict of interest** | mitigated | Schema trigger: nobody is judge and participant in one event. Assignments skip team members; a competitor cannot accept a judge invite. | Friendships outside the platform. Organizers can remove assignments. |
| **Collusion between judges** | partial | Scores are sealed until publication (no coordinating on numbers). The judge-bias model damps a single inflated judge; the calibration table exposes outliers (flat scorers, extreme biases); overlap-diverse assignment means colluders rarely share many projects. | Coordinated inflation by several judges of one track survives normalization. Only human review of the calibration table catches it. |
| **Harsh or generous judges** | mitigated | Documented normalization (JUDGING.md) with a planted-bias proof. | Spread differences are corrected only in the z-score and BT views. |
| **Judge repudiation** ("I never scored that") | mitigated | Audit trail entries plus a signed judge record with a SHA-256 commitment to the exact scorecards. | – |

### Submissions and deadlines

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| **Cutoff gaming** (edit after the deadline, forge a timestamp) | mitigated | One guard on every write path, HTML and API, using the server clock only. No client time is ever read. Refused attempts are audited and listed on the integrity page. `test_deadline_is_enforced_to_the_second` | A request that *starts* before the cutoff and commits a moment after is accepted (the check runs at request start). |
| **Duplicate submissions** | mitigated | Detection by normalized repo, normalized title and description similarity; organizer dismisses or withdraws. `test_integrity.py` | Paraphrased clones with a different repository. |
| **Taking over someone's project** | mitigated | Edits require team membership; teams lock at the deadline; the captain controls membership. `test_other_people_cannot_edit` | A captain who removes a teammate before the deadline: an intended power, audited. |
| **Malicious uploads** | mitigated | Magic-byte typing, SVG refused, 2 MB cap, random names, `nosniff`, sandboxing CSP on `/uploads`. | Image-parser bugs in browsers. |

### Web and accounts

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| **CSRF** | mitigated | Global double-submit check for forms, Origin check for all unsafe requests, SameSite=Lax cookies. JSON needs a CORS preflight we never grant. `test_auth.py` | – |
| **XSS** | mitigated | Autoescaping everywhere, http(s)-only links (validated on input, filtered on output, including imported data), strict CSP (`script-src 'self'`, no inline handlers). `test_logged_in_users_comment_and_text_is_escaped` | CSP allows inline *styles* (used for progress widths). |
| **Clickjacking** | mitigated | `frame-ancestors 'none'` and `X-Frame-Options: DENY` everywhere except the read-only `/embed/*`. | – |
| **Session theft** | partial | HttpOnly, SameSite, tokens hashed at rest, rotation at login, "sign out others", reset revokes all sessions. | Plain HTTP in the default compose file. Put TLS in front and set `DOGFOOD_COOKIE_SECURE=1`. |
| **Brute force / credential stuffing** | partial | scrypt; login rate limits per IP (30/5 min) and per account (8/15 min). | Limits live in memory: they reset on restart and are not shared across processes. |
| **Account enumeration** | partial | Login and reset give identical answers and equal timing. | Registration says an email is taken (a UX choice), and it is rate-limited. |
| **CSV / formula injection** | mitigated | Cells starting with `= + - @` are neutralised (numbers pass through). `test_formula_cells_are_neutralised` | – |
| **SSRF via webhooks** | partial | Organizer-only, http(s) only, 5 s timeout, redirects never followed (a 3xx counts as a failed delivery). `DOGFOOD_WEBHOOK_BLOCK_PRIVATE=1` refuses private and loopback targets. | Off by default (self-hosters often run bots locally); DNS rebinding between check and delivery is not covered. |
| **Scraping** | partial | The gallery is public *by design*. Drafts, emails, scores and voters are never on public pages. The embed shows submitted projects only. | Nothing stops scraping public pages; there is nothing private on them. |
| **Denial of service** | unmitigated | Rate limits on the expensive writes only. | Put a reverse proxy with connection limits in front for a public deployment. |

### Integrity of the record

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| **Rewriting history in the database** | partial | The audit log is append-only (triggers) and hash-chained. `verify_chain` pinpoints the first altered row and the badge shows on admin and organizer pages. `test_audit_chain_verifies_and_detects_tampering` | Someone with write access can drop triggers and rebuild the *whole* chain. To be tamper-evident against the host too, publish the latest hash elsewhere (a webhook or a screenshot on results day). |
| **Forged certificates** | mitigated | Ed25519 signatures over canonical JSON; a public key endpoint; an offline verifier; revocation is visible. `test_records.py` | Loss of the private key: back up `keys/`. |
| **Organizer rigging** | partial | Every results-affecting action (weights, assignments, method, publish, withdrawals, voids) is audited in plain English. Once results are published, weights, method and withdrawals are frozen until an audited unpublish. Results are recomputed from stored scorecards, so anyone with an export can re-run the method. | Organizers are trusted operators; the log makes abuse visible, not impossible. |

### Supply chain and operations

| Threat | Status | Defenses | Residual risk |
|---|---|---|---|
| Malicious dependency | partial | Five pinned runtime dependencies, wheels built once at image build. | No hash pinning (`--require-hashes`) yet. |
| Secrets in the repository | mitigated | The secret key and signing key are generated at first boot into the data volume. Demo credentials exist only with `DOGFOOD_DEMO=1`. | Operators must not run demo mode in public. The README says so. |

## Deliberately out of scope

- Proof-of-personhood for community votes. It needs identity infrastructure
  this offline, self-hosted portal cannot assume.
- Anonymous-yet-verifiable ballots (blind signatures, zero-knowledge proofs).
- Protection from a malicious host operator beyond tamper *evidence*.
