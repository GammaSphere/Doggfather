# Demo: the five-minute run-through

The DOGFOOD brief asks for a video of about five minutes showing **one full
event lifecycle: create, submit, judge, publish**. This is the script for it:
what to prepare, what to click, what to say, and how long each part takes.

Every step below is executed by an automated test,
[`tests/test_demo_walkthrough.py`](tests/test_demo_walkthrough.py), with the
same names and values. If that test passes, this script works.

---

## Before you record

**Start from a clean portal.** `down -v` deletes the portal's data volume;
the next boot reseeds it from `fixtures.json`.

```bash
docker compose down -v
```

```bash
docker compose up
```

Wait for the banner (`seeded. test logins: …`). Keep that terminal visible;
the first shot is the banner.

**Open three browser windows,** so you never have to log out on camera:

| Window | Who | How to log in (password `dogfood-demo-2026` for all) |
|---|---|---|
| **A**, normal window | Olu, the organizer | `organizer@doggfather.local` |
| **B**, private/incognito window | Sam, a new participant | registers during the demo |
| **C**, a second browser | Jonas, a judge | `jonas.vogel@example.org` |

Log A and C in before recording. Leave B on the home page. Browser zoom around
90% at 1440×900 or larger keeps the organizer tables readable.

**Have a second terminal ready** in the repository folder for the closing
`python3 run.py .dogfood.toml` and the two `curl` lines in act 5.

**Rehearse once.** The whole run takes about 4½ minutes at a calm pace.

All times on screen are UTC.

---

## The script

### Act 1 (0:00–0:35): boot and the public side

| Do | Say |
|---|---|
| Show the terminal: `docker compose up` and the seeded banner. | "One command, no network needed. It boots with the official DOGFOOD fixtures: 41 projects, 30 judges, 126 scorecards." |
| Window B: open http://localhost:8080. Scroll past the hero. | "This is Doggfather, a self-hosted hackathon platform." |
| Click **Gallery**, type `harbour`, press Go, then tick two **Tracks**. | "The gallery is public, with search and multi-filters. Note the fixture's duplicate submission: two *Dry Harbour* entries." |

### Act 2 (0:35–1:20): create an event (organizer, window A)

| Do | Say |
|---|---|
| **Organize → New event**. Name `Demo Night`, tagline `One evening, one prize`. Leave the dates as proposed. **Create event**. | "Organizers set every phase date: submissions, deadline, judging, and an optional community vote." |
| On the event, open **Settings**. Under **Tracks** add `AI tools`, then `Civic tech`. Under **Prizes** add `Best in show`, value `$500`. | "Tracks, prizes and custom submission questions are all per event." |
| Back to **Overview**, click **Open submissions now**, confirm. | "Every phase button is an ordinary, audited date change." |

### Act 3 (1:20–2:20): form a team and submit (participant, window B)

| Do | Say |
|---|---|
| **Log in → Create an account**: `Sam Builder`, `sam@example.org`, any password of 10+ characters. | "Participants bring their own account." |
| **Events → Demo Night → Join or form a team**. Team name `Night Owls`, **Create team**. Point at the invite link and **Copy**. | "Teams form by invite link, with a size cap and one team per person per event." |
| **Start a submission**: title `Lamplight`, **Create draft**. Fill tagline `Finds the dark corners of your dashboards`, pick track `AI tools`, a two-sentence description, repo `https://example.org/lamplight`, tags `python fastapi`, and upload any PNG as the thumbnail. **Save & submit**. | "Drafts are private. Submitted projects go live in the gallery and stay editable until the deadline. After it, the server refuses every change, whatever the client sends." |
| Open **Gallery**: Lamplight is there. | |

### Act 4 (2:20–3:20): judging (windows A and C)

| Do | Say |
|---|---|
| Window A: **Judges**. Email `jonas.vogel@example.org`, tick **AI tools**, **Send invite**. | "Judges are invited with a track scope. Offline, the mail lands in the local outbox, and the organizer also gets the link to share directly." |
| Click **Copy** on the invite link. In window C (Jonas), paste it into the address bar and click **Accept invitation**. | "One click, and Jonas is a judge who can only ever see the AI tools track." |
| Window A: **Overview → Close submissions now**, confirm. Then **Assignments → Run auto-assignment**. | "The assigner balances load and spreads overlap between judges, which the normalization needs." |
| Window C: Jonas landed on his (still empty) queue when he accepted. Reload it: Lamplight is there. Click **Score next project**, press `4`, `3`, `5`, then **Enter**. | "Scoring is keyboard-first. Digits score and advance, Enter saves and opens the next project. That matters when a judge has 30 projects." |

### Act 5 (3:20–4:10): normalization and isolation (fixture event)

| Do | Say |
|---|---|
| Window A: open **Organize → Sample Hack 2026 → Results**. | "Now the fixture event, with 126 real scorecards." |
| Click through the four method tags: **Raw mean**, **Shrunk z-score**, **Judge-bias model**, **Within-judge Bradley–Terry**. Scroll to **Judge calibration**; point at *Iva Petrova · flat scorer*. | "Judges are harsh or generous. We fit a judge-bias model, ridge-regularised so a judge with one card is barely corrected, and show rank movements against the raw mean. JUDGING.md has the maths, and a simulation where it beats raw averages in 40 of 40 events." |
| Open **Progress**. | "Live progress, delinquent judges and abandoned batches." |
| Terminal: the two `curl` lines below. | "Isolation lives in the backend: judge B asking for judge A's scores gets 403, not a hidden button." |

```bash
curl -i -H "Cookie: session=jdg_b_44de" http://localhost:8080/api/judges/jdg_26/scores
```

```bash
curl -s -H "Cookie: session=jdg_a_91bc" http://localhost:8080/api/judge/scores
```

### Act 6 (4:10–5:00): publish, certify, verify

| Do | Say |
|---|---|
| Window B: open http://localhost:8080/events/demo-night/results. It shows **Sealed**. | "Until the organizers publish, nobody sees results, judges included." |
| Window A: **Demo Night → Overview → Close judging now**, then **Results → Publish results**. Reload window B: the ranking is public. | |
| Window A: **Records → Issue certificates**. Open Sam's certificate: **Signature valid**. | "Certificates and judge records are Ed25519-signed and verifiable offline." |
| Terminal: `python3 run.py .dogfood.toml` | "And the official DOGFOOD checker: seven out of seven." |

---

## Shorter variant (if you are over time)

- Act 4: invite Jonas before recording (Judges → invite → accept), then start
  the act at **Close submissions now**. This saves about 30 seconds.
- Act 5: show only the **Judge-bias model** tag and the calibration table.

## Extended cut (optional, for a longer video)

| Feature | Where |
|---|---|
| Quadratic community vote (email-gated) | Window B: http://localhost:8080/vote/sample-hack-2026 → enter an email; the code is in **Admin → Outbox** (as admin). Spend 25 credits: 3 votes cost 9. |
| Pairwise judging | Window A: Sample Hack 2026 → **Settings** → tick *Pairwise judging mode* → Save. Window C: **Judge → Pairwise mode**; use ← and →. |
| REST API | http://localhost:8080/api/docs, then **Dashboard → API tokens** to create one. |
| Webhooks | Organizer → **Webhooks** → add one, then **Send ping** and watch the delivery log. |
| Embeddable gallery | Organizer → **Embed**: the snippet and a live preview. |
| Full export / import | Organizer → **Exports → Download bundle**; `python -m doggfather export evt_01 out.json`. |
| Deadline holding | `curl -i -X POST -H "Cookie: session=prt_2e88" -H "Content-Type: application/json" -d '{"title":"late"}' http://localhost:8080/projects/new` returns `403 submissions_closed`. |
| Audit trail | Organizer → **Audit**: every step of this demo, in plain English, hash-chained. |

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `port is already allocated` | Something else uses 8080. Run `DOGFOOD_PORT=18080 docker compose up` and use port 18080 in the URLs and curl lines. |
| Jonas's queue is empty after auto-assignment | The invite did not include the **AI tools** track, or submissions were still open. Close submissions, then run auto-assignment again (it is safe to repeat). |
| "Submissions for this event have not opened yet" | You skipped **Open submissions now** in act 2. |
| Leftover data from a previous take | `docker compose down -v`, then `docker compose up`. |
| Results page says **Sealed** after publishing | Reload; publishing needs judging closed first (**Close judging now**). |

## What the video proves, by judging criterion

| Criterion | Shown in |
|---|---|
| Tier completion and correctness (40%) | Acts 2–4 and 6 (T1 and T2 end to end), the extended cut (T3 and T4), the checker at the end |
| Judging integrity (25%) | Act 5: normalization with calibration, 403 isolation, sealed results, audit trail |
| Adoptability (20%) | Act 1: one command, seeded, offline; bundles in the extended cut |
| Code quality and innovation (15%) | Keyboard scoring, signed judge records, the assigner that keeps judges connected |
