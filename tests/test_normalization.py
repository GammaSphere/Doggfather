"""T2 + Normalization Proof bonus.

Unit tests pin down the maths. The simulation test is the proof: with judge
bias planted on purpose, the normalized ranking recovers the true ranking
better than the raw mean, on every one of a batch of random events.
"""

import math
import random
import statistics

import pytest

from doggfather.services.normalization import (
    METHODS, Observation, compute, judge_bias_model, rank, spearman, zscore_shrunk,
)
from doggfather.services.pairwise import bradley_terry
from doggfather.services.events import get_event
from tests.helpers import post_form

SLUG = "sample-hack-2026"


# --------------------------------------------------------------- the maths

def test_bias_model_recovers_planted_bias_exactly_without_noise():
    quality = {"p1": 4.0, "p2": 3.0, "p3": 2.5, "p4": 2.0}
    bias = {"harsh": -1.0, "fair": 0.0, "generous": 0.8}
    obs = [Observation(j, p, q + b) for p, q in quality.items() for j, b in bias.items()]
    fit = judge_bias_model(obs, lam=1e-9)
    assert rank(fit.scores) == rank(quality)
    assert fit.scores["p1"] - fit.scores["p4"] == pytest.approx(2.0, abs=1e-6)
    learned = {j: d["bias"] for j, d in fit.judge_detail.items()}
    assert learned["generous"] - learned["harsh"] == pytest.approx(1.8, abs=1e-6)


def test_strong_batch_is_not_mistaken_for_generosity():
    # Judge "a" happened to draw the three best projects. Judge "b" saw
    # everything, overlapping with "a" on p1-p3. Nobody is biased.
    quality = {"p1": 4.6, "p2": 4.4, "p3": 4.2, "p4": 2.4, "p5": 2.2, "p6": 2.0}
    obs = [Observation("b", p, q) for p, q in quality.items()]
    obs += [Observation("a", p, quality[p]) for p in ("p1", "p2", "p3")]
    fit = judge_bias_model(obs, lam=0.5)
    assert rank(fit.scores) == rank(quality)
    assert abs(fit.judge_detail["a"]["bias"]) < 1e-9  # shared work proves "a" is not generous


def test_flat_judge_carries_no_ranking_signal_and_no_division_by_zero():
    obs = [Observation("flat", p, 4.0) for p in ("p1", "p2", "p3")]
    obs += [Observation("other", "p4", 2.0), Observation("other", "p5", 3.0)]
    fit = zscore_shrunk(obs)
    assert all(math.isfinite(v) for v in fit.scores.values())
    assert fit.scores["p1"] == fit.scores["p2"] == fit.scores["p3"]
    assert fit.judge_detail["flat"]["sd"] == 0 and fit.judge_detail["flat"]["shrunk_sd"] > 0


def test_bradley_terry_orders_a_chain_and_tames_undefeated_items():
    comparisons = [("a", "b", 1.0)] * 3 + [("b", "c", 1.0)] * 3
    theta = bradley_terry(comparisons)
    assert theta["a"] > theta["b"] > theta["c"]
    assert all(math.isfinite(v) for v in theta.values())  # "a" never lost, yet stays finite
    assert sum(theta.values()) == pytest.approx(0.0, abs=1e-9)


def test_bradley_terry_treats_ties_symmetrically():
    theta = bradley_terry([("x", "y", 0.5), ("y", "x", 0.5)])
    assert theta["x"] == pytest.approx(theta["y"])


def test_competition_ranking_shares_ranks_on_exact_ties():
    assert rank({"a": 3.0, "b": 3.0, "c": 1.0}) == {"a": 1, "b": 1, "c": 3}


def test_spearman_bounds():
    xs = {k: float(i) for i, k in enumerate("abcdef")}
    assert spearman(xs, xs) == pytest.approx(1.0)
    assert spearman(xs, {k: -v for k, v in xs.items()}) == pytest.approx(-1.0)


# ----------------------------------------------------------- the proof

def simulate(seed, n_projects=40, n_judges=24, n_tracks=6, k=3, bias_sd=0.7, noise_sd=0.35):
    """A random hackathon: true quality per project, a hidden bias per judge,
    track-limited assignment of k judges per project, noisy clamped scores."""
    rng = random.Random(seed)
    truth = {f"p{i:02d}": rng.gauss(3.0, 0.6) for i in range(n_projects)}
    track = {p: i % n_tracks for i, p in enumerate(truth)}
    judges = [f"j{i:02d}" for i in range(n_judges)]
    cover = {j: {i % n_tracks, rng.randrange(n_tracks)} for i, j in enumerate(judges)}
    bias = {j: rng.gauss(0, bias_sd) for j in judges}
    load = dict.fromkeys(judges, 0)
    obs = []
    for p in truth:
        eligible = [j for j in judges if track[p] in cover[j]]
        rng.shuffle(eligible)
        for j in sorted(eligible, key=lambda name: load[name])[:k]:
            load[j] += 1
            obs.append(Observation(j, p, min(5.0, max(1.0, truth[p] + bias[j] + rng.gauss(0, noise_sd)))))
    return truth, obs


def test_normalization_recovers_true_ranking_better_than_raw_means():
    seeds = range(20)
    rho = {m: [] for m in METHODS}
    for seed in seeds:
        truth, obs = simulate(seed)
        for m in METHODS:
            rho[m].append(spearman(compute(m, obs).scores, truth))
    raw = statistics.fmean(rho["raw"])
    bias = statistics.fmean(rho["bias"])
    assert bias > raw + 0.05, (raw, bias)
    assert sum(b > r for b, r in zip(rho["bias"], rho["raw"])) >= 18
    assert statistics.fmean(rho["zscore"]) > raw
    assert statistics.fmean(rho["pairwise"]) > raw


# ---------------------------------------------------- on the fixture data

def test_organizer_sees_all_methods_and_judge_flags(as_role):
    org = as_role("organizer")
    html = org.get(f"/organize/{SLUG}/results").text
    assert "Judge calibration" in html and "flat scorer" in html  # jdg_07 gave 4/4/4 three times
    assert "Iva Petrova" in html
    for method in METHODS:
        assert org.get(f"/organize/{SLUG}/results?method={method}").status_code == 200


def test_results_api_for_organizer_has_every_project_and_method(as_role):
    body = as_role("organizer").get("/api/events/evt_01/results").json()
    assert body["method"] == "bias" and len(body["results"]) == 41
    first = body["results"][0]
    assert first["rank"] == 1 and {"raw", "zscore", "bias", "pairwise", "movement"} <= set(first)


@pytest.mark.parametrize("role", [None, "participant", "judge_a"])
def test_results_are_sealed_until_published(as_role, role):
    client = as_role(role)
    assert client.get(f"/events/{SLUG}/results").status_code == 403
    response = client.get("/api/events/evt_01/results")
    assert response.status_code == 403 and response.json()["error"] == "results_hidden"


def test_publish_requires_closed_judging_then_opens_results(as_role, conn):
    org = as_role("organizer")
    page = f"/organize/{SLUG}/results"
    post_form(org, page, {"action": "publish"}, token_from=page)
    assert get_event(conn, "evt_01").results_published_at is None  # judging still open

    post_form(org, f"/organize/{SLUG}/phase", {"action": "close_judging"}, token_from=f"/organize/{SLUG}")
    post_form(org, page, {"action": "publish"}, token_from=page)
    assert get_event(conn, "evt_01").results_published_at is not None

    visitor = as_role(None)
    assert visitor.get(f"/events/{SLUG}/results").status_code == 200
    body = visitor.get("/api/events/evt_01/results").json()
    assert "raw" not in body["results"][0]  # the public sees the published ranking, not the workings


def test_changing_weights_changes_results_without_touching_scores(as_role, conn):
    org = as_role("organizer")
    before = org.get("/api/events/evt_01/results?method=raw").json()["results"]
    stored = conn.execute("SELECT SUM(value) FROM score_items").fetchone()[0]
    conn.execute("UPDATE criteria SET weight = 5 WHERE id = 'evt_01:innovation'")
    after = org.get("/api/events/evt_01/results?method=raw").json()["results"]
    assert [r["id"] for r in before] != [r["id"] for r in after]
    assert conn.execute("SELECT SUM(value) FROM score_items").fetchone()[0] == stored


def test_public_method_page(as_role):
    assert "judge-bias model" in as_role(None).get("/judging").text.lower()
