# Judging

How Doggfather turns judges' opinions into a ranking, and why each step is
built the way it is. Every number quoted here comes from
[`docs/normalization-report.md`](docs/normalization-report.md), which is
regenerated from `fixtures.json` by a script, and every property claimed is
exercised by a test in [`tests/`](tests/).

```
assignment ──► scoring ──► normalization ──► ranking ──► publication ──► signed records
 (who reviews)   (rubric)    (remove judge bias)  (ties)     (sealed until)     (verifiable)
```

---

## 1. Assignment: who reviews what

Organizers can hand-pick a **batch** (a judge and a list of projects) or run
the **automatic** assigner, which tops every submitted project up to the
event's review target *k* (default 3):

1. Visit projects **fewest-reviews-first**, so shortfalls land on
   well-covered projects, not starved ones.
2. Candidates are the judges who **cover the project's track** and are not
   already on it. A judge on the project's team is impossible: the schema
   forbids being a judge and a participant in the same event.
3. Pick the candidate with, in order:
   - the **lightest load** (bounds the slowest judge, spreads fatigue),
   - the **fewest projects already shared** with the judges on this project,
     which spreads overlap across many judge pairs instead of forming cliques.
     This is what makes normalization work: the bias model can only compare
     two judges through projects they both reviewed, so the judge graph must
     be **connected**,
   - a **stable hash** of (seed, judge, project) for remaining ties. Re-running
     is deterministic and idempotent.
4. A project that cannot reach *k* (too few judges in its track) is reported as
   a **shortfall**; it is never quietly filled from another track.

On the fixtures, the 126 imported scorecards already connect all 30 judges
into **one component**. Eight projects have only two reviews (the "unfinished
batches"). The seed runs the assigner once, and it creates exactly 8 pending
third-review assignments, one per short project, all within track. The
progress dashboard then shows that outstanding work as it would during a live event.

Tests: `tests/test_judging_setup.py`.

## 2. Scoring

Each judge scores each assigned project on every rubric criterion, 1–5 by
default. A scorecard's **weighted total** on the rubric's own scale is

```
weighted = Σ_c w_c · s_c  /  Σ_c w_c        (criteria with w_c > 0)
```

- Weights are organizer-configurable per event, with **per-track
  overrides** (a security track can value quality differently from an
  education track).
- Weights are applied **when results are computed**, never when scores are
  stored. Re-weighting mid-event changes no stored data and is audited.
- Once the first score lands, the **scale and the set of criteria freeze**
  (service check plus database triggers on `score_items`), because changing
  them would make stored scores ambiguous.
- The fixture event uses its three criteria (functionality, quality,
  innovation) with equal weights; the fixture specifies none, and inventing
  weights would bias the comparison.

**Review fatigue.** Industry numbers put 30 projects at about 5 hours of
judging. The scoring screen puts the project and the rubric side by side.
Digits score the active criterion and advance, and Enter saves and opens the
next pending project, so a scorecard is a handful of keystrokes. The queue
shows progress and a time estimate. Judges see only their own work.

## 3. Normalization: removing judge bias

### The problem

Judges differ in **location** (harsh or generous) and **spread** (one uses
1–5, another only 3–4). With 2–5 reviewers per project, who you happened to
draw can move you several places. The fixtures contain the cases that break
naive fixes:

| Case | In the fixtures | Why it breaks naive methods |
|---|---|---|
| Flat scorer | Iva Petrova (jdg_07): 4/4/4 on all three projects | Plain z-scoring divides by a standard deviation of 0 |
| One-review judges | Tomas Varga (jdg_01), Anya Sokolova (jdg_23) | Any per-judge statistic from one scorecard is noise |
| Uneven coverage | 2 to 5 reviews per project | The raw mean rewards lucky draws |
| Strong batch | Track-limited judges see different project pools | Z-scoring assumes each judge saw an average batch |

### Four methods, side by side

The organizer's results page computes all four and shows every project's rank
under each, with its movement against the raw mean. The organizer picks one
for publication.

**Raw mean.** The mean weighted total. The baseline.

**Shrunk z-score.** Standardize each judge against their own mean *m_j*
and variance *v_j*, but shrink both toward the pool in proportion to how
little we know (*k* = 3):

```
m'_j = (n_j·m_j + k·M) / (n_j + k)          M   = grand mean
v'_j = (n_j·v_j + k·S²) / (n_j + k)          S²  = pooled within-judge variance
z    = (x − m'_j) / √v'_j
score_p = M + S · mean(z over p's reviews)   (back on the rubric scale)
```

The flat scorer has *v_j* = 0 but *v'_j* > 0, so there is no division by zero.
Their three projects receive identical *z*, so they carry no ranking signal.
A one-review judge is pulled mostly back to the pool.

**Judge-bias model (default).** An additive model fitted to every scorecard:

```
s_jp = μ + a_p + b_j + ε

minimise   Σ (s_jp − μ − a_p − b_j)²  +  λ Σ_j b_j²        (λ = 2)
score_p = μ + a_p
```

This is ridge-penalised least squares, the BLUP of a crossed random-effects
model with a fixed variance ratio. It is solved by block coordinate descent,
alternating closed-form updates for *a* and *b*; convergence is guaranteed
because the objective is strictly convex in *b*.

**Within-judge Bradley–Terry.** Every pair of projects scored by the same
judge becomes a comparison: the higher total wins, and a tie is half a win each.
BT strengths are fitted by the MM algorithm (Hunter 2004) with a weak
symmetric prior, so an undefeated project stays finite. Only the *order*
inside each judge's batch is used, so any per-judge shift or stretch cancels
exactly. Direct verdicts from pairwise mode (§5) join the same fit.

### Why the judge-bias model is the default

1. **It uses the overlap.** If Jonas and Diego reviewed the same five
   projects and Jonas is consistently higher, the model learns that from the
   shared work. Z-scoring cannot: it assumes every judge drew an average
   batch, so a judge who happened to draw the three best projects gets
   "corrected" downward. `tests/test_normalization.py::test_strong_batch_is_not_mistaken_for_generosity`
   shows the bias model estimating exactly zero bias for such a judge.
2. **No evidence, no correction.** The ridge term shrinks the bias of a
   judge with *n* reviews by roughly *n/(n+λ)*. Tomas Varga's single 2/2/2
   card earns a −0.45 correction, not the −1.6 that comparing it with the grand mean
   (3.57) would imply.
3. **The output stays on the rubric scale** (score = μ + a_p), so published
   numbers mean what people expect: "about 4.3 out of 5".
4. **It degrades gracefully.** With no overlap (a disconnected judge graph)
   the penalty pins biases near zero and the method falls back toward raw
   means rather than inventing corrections. The results page reports the
   number of connected components (1 on the fixtures).

What it does not do: it corrects **location**, not **spread**. A judge who
uses only 3–4 still counts for less than one who uses 1–5. We accept that
because spread differences on a 5-point scale are small next to location
differences. Where spread matters, the z-score and Bradley–Terry columns are
right there for comparison.

### Results on the fixtures

From [`docs/normalization-report.md`](docs/normalization-report.md):

- 126 scorecards, 30 judges, 41 projects, **one connected judge component**.
- Agreement with the raw ranking (Spearman ρ): shrunk z-score 0.966,
  judge-bias model 0.960, within-judge BT 0.816. The corrections are real but
  proportionate.
- Most generous judges after correction: Wei Lindqvist +0.39, Yuki Sato +0.37;
  harshest: Tomas Varga −0.45 (one card, heavily shrunk), Hiro Tanaka −0.39.
  The flat scorer Iva Petrova is estimated +0.26: her 4s run slightly above
  what her co-reviewers gave the same projects.
- Largest movements, with their causes traced in the calibration table:
  Small Meadow falls 10 places (all three of its reviewers score above the
  norm: +0.15, +0.06, +0.26). Small Relay falls 6 (one of its two reviewers is
  the flat scorer, whose 4s run generous). Glass Beacon climbs 5 (both of its
  reviewers lean harsh: −0.10, −0.09).
- **Stability:** across λ from 0.5 to 5 the top 10 is unchanged, and ρ against
  λ = 2 stays ≥ 0.98. The default is not a knife-edge choice.

### The proof: planted bias

A method that "changes the ranking" is not thereby correct. To test
correctness we need a ground truth, so we simulate it: 40 events, each with 40
projects of hidden true quality, 24 judges with hidden bias ~ N(0, 0.7) and
noise ~ N(0, 0.35), 6 tracks, and 3 track-limited reviews per project,
clamped to the 1–5 scale. Then we measure how well each method recovers the
**true** ranking:

| Method | mean ρ vs truth | worst event | beats raw |
|---|---:|---:|---:|
| Raw mean | 0.802 | 0.599 | – |
| Shrunk z-score | 0.878 | 0.790 | 40/40 |
| **Judge-bias model** | **0.883** | 0.703 | **40/40** |
| Within-judge Bradley–Terry | 0.859 | 0.754 | 35/40 |

`tests/test_normalization.py::test_normalization_recovers_true_ranking_better_than_raw_means`
asserts this on every test run (≥ 18/20 wins and ≥ 0.05 mean improvement).
Smaller tests pin the maths: exact bias recovery without noise, finite
Bradley–Terry strengths for an undefeated project, and no division by zero
for flat scorers.

### Ties

Competition ranking (1, 2, 2, 4) on the chosen score. Display order within an
exact tie breaks on raw mean, then number of reviews, then earlier submission.
The rank itself stays shared, because a tie in the evidence should not be
hidden by an arbitrary rule.

## 4. Isolation: what each role can see

Enforced in the backend: `policy.py` holds the matrix, and judge queries carry
`judge_id` in their SQL `WHERE` clause.

| Actor | Own scores | Peer scores | Other tracks | Aggregates | Audit log |
|---|---|---|---|---|---|
| Visitor | – | – | – | after publication | – |
| Participant | – | – | – | after publication | – |
| Judge | ✓ | 403 | 403 | after publication | – |
| Organizer | ✓ | ✓ | ✓ | ✓ | ✓ |
| Admin | ✓ | ✓ | ✓ | ✓ | ✓ |

- `GET /api/judges/<id>/scores` returns 403 for any other judge, including
  ids that do not exist, so judges cannot enumerate peers.
- A judge can open or score a project only if it is in their queue and
  their tracks.
- Results and community tallies return `403 results_hidden` until an
  organizer publishes (which requires judging to be closed).

Tests: `tests/test_isolation.py`, and the official checker via `tests/test_contract.py`.

## 5. Pairwise mode (optional)

When enabled, judges can also answer "which of these two is better?", the
question Gavel (HackMIT) built its judging around, because it needs no
calibrated scale.

- **Active pair selection.** Take the least-compared project in the judge's
  pool and pair it with the untried opponent whose current BT strength is
  closest (a near coin-flip is the most informative duel). Left and right are
  assigned by a stable hash, so screen position cannot bias the verdict.
- A judge compares any pair at most once (unique index). The pool follows
  the scoring isolation rules.
- Direct verdicts are added to the scorecard-implied comparisons in the
  Bradley–Terry method, and organizers also see a ranking from direct
  verdicts alone.
- Not implemented: Crowd-BT's per-judge reliability weights. Every verdict
  counts equally; a judge who clicks randomly adds noise, not bias.

Tests: `tests/test_pairwise.py`.

## 6. Community vote (T3)

Quadratic voting: each voter has *C* credits (default 25) and *v* votes on
one project cost *v²*, so broad support beats a single enthusiast. Ballots
appear in a stable per-voter shuffle (HMAC of the voter identity) to cancel
top-of-page bias. Tallies are sealed until voting closes and results are
published. Gates, Sybil defenses and their limits are in
[THREAT-MODEL.md](THREAT-MODEL.md). The judges' ranking and the community
tally are reported separately; they are never blended.

## 7. Accountability

- Every organizer action that affects results (weights, assignments,
  publication, withdrawals, voids) is written to a **hash-chained,
  append-only audit log**, readable as plain sentences and verified on demand.
- After judging closes, organizers issue **Ed25519-signed judge records**.
  Each states the reviews filed and a SHA-256 commitment to the judge's exact
  scorecards. The judge can later reveal the scorecards and anyone can check
  them against the commitment. Until then the record discloses nothing about
  how they scored.

## Reproduce

```
python -m doggfather.tools.normalization_report fixtures.json > docs/normalization-report.md
pytest tests/test_normalization.py tests/test_isolation.py tests/test_judging_setup.py tests/test_pairwise.py
```
