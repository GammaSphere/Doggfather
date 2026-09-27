"""Cross-judge score normalization (pure functions, no database).

Input: one observation per filed scorecard, the weighted total on the
rubric scale. Output: a score per project under four methods, so an
organizer can see how much each correction moves the ranking.

``raw``
    Mean of the project's weighted totals. The baseline: it silently rewards
    projects that happened to draw generous judges.

``zscore`` (shrunk per-judge standardisation)
    Each judge's scores are centred and scaled by that judge's own mean and
    spread, but both are shrunk toward the pooled values in proportion to
    how little we know. A judge with n scores gets weight n/(n+k), k=3:

        m'_j = (n_j*m_j + k*M) / (n_j + k)
        v'_j = (n_j*v_j + k*S^2) / (n_j + k)
        z    = (x - m'_j) / sqrt(v'_j)
        score_p = M + S * mean(z over p's reviews)

    M is the grand mean and S^2 the pooled within-judge variance. A judge who
    gave every project the same score (fixture jdg_07: 4/4/4) has v_j = 0 but
    v'_j > 0, so there is no division by zero; their reviews simply carry no
    ranking signal. A one-review judge (jdg_01) is mostly pulled back to
    the pool.

``bias`` (additive judge-effect model, default)
    s_jp = mu + a_p + b_j + e, fitted by ridge-penalised least squares:

        minimise  sum (s_jp - mu - a_p - b_j)^2  +  lambda * sum b_j^2

    Unlike z-scoring, this uses the overlap between judges: if judge A and
    judge B both reviewed the same projects and A is consistently 0.8
    lower, the model learns A's bias from the shared work, instead of
    assuming every judge drew an average batch. The ridge term (lambda=2)
    shrinks the bias estimate of a judge with few reviews toward zero:
    no evidence, no correction. It is the BLUP of a crossed random-effects
    model with a fixed variance ratio. Solved by block coordinate descent,
    which converges because the objective is strictly convex in b.
    score_p = mu + a_p, on the rubric scale.

``pairwise`` (within-judge Bradley-Terry)
    Every pair of projects scored by the same judge becomes a comparison
    (higher total wins, a tie is half a win each). Only the *order* inside
    one judge's batch is used, so any per-judge shift or stretch, however
    extreme, cancels out entirely. Output is a log-strength, not a
    rubric-scale score.

JUDGING.md defends the default and shows all four on the fixture data.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from .pairwise import bradley_terry

METHODS = ("raw", "zscore", "bias", "pairwise")
METHOD_LABELS = {
    "raw": "Raw mean",
    "zscore": "Shrunk z-score",
    "bias": "Judge-bias model",
    "pairwise": "Within-judge Bradley-Terry",
}
SHRINK_K = 3.0
BIAS_LAMBDA = 2.0


@dataclass(frozen=True)
class Observation:
    judge: str
    project: str
    score: float


@dataclass
class MethodResult:
    method: str
    scores: dict[str, float]
    judge_detail: dict[str, dict[str, float]] = field(default_factory=dict)
    info: dict[str, float] = field(default_factory=dict)


def _by(obs: Iterable[Observation], key: str) -> dict[str, list[Observation]]:
    groups: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        groups[getattr(o, key)].append(o)
    return groups


def raw_mean(obs: list[Observation]) -> MethodResult:
    scores = {p: statistics.fmean(o.score for o in rows) for p, rows in _by(obs, "project").items()}
    return MethodResult("raw", scores)


def pooled_within_variance(obs: list[Observation]) -> float:
    by_judge = _by(obs, "judge")
    squares = sum((o.score - statistics.fmean(x.score for x in rows)) ** 2 for rows in by_judge.values() for o in rows)
    dof = len(obs) - len(by_judge)
    if dof > 0 and squares > 0:
        return squares / dof
    return statistics.pvariance([o.score for o in obs]) if len(obs) > 1 else 1.0


def zscore_shrunk(obs: list[Observation], k: float = SHRINK_K) -> MethodResult:
    if not obs:
        return MethodResult("zscore", {})
    grand = statistics.fmean(o.score for o in obs)
    pooled_var = pooled_within_variance(obs) or 1.0
    pooled_sd = math.sqrt(pooled_var)
    detail: dict[str, dict[str, float]] = {}
    z_by_project: dict[str, list[float]] = defaultdict(list)
    for judge, rows in _by(obs, "judge").items():
        n = len(rows)
        mean = statistics.fmean(o.score for o in rows)
        var = statistics.pvariance([o.score for o in rows]) if n > 1 else 0.0
        mean_s = (n * mean + k * grand) / (n + k)
        var_s = (n * var + k * pooled_var) / (n + k)
        sd_s = math.sqrt(var_s) if var_s > 0 else pooled_sd
        detail[judge] = {"n": n, "mean": mean, "sd": math.sqrt(var), "shrunk_mean": mean_s, "shrunk_sd": sd_s}
        for o in rows:
            z_by_project[o.project].append((o.score - mean_s) / sd_s)
    scores = {p: grand + pooled_sd * statistics.fmean(zs) for p, zs in z_by_project.items()}
    return MethodResult("zscore", scores, detail, {"grand_mean": grand, "pooled_sd": pooled_sd, "k": k})


def judge_bias_model(obs: list[Observation], lam: float = BIAS_LAMBDA, iterations: int = 5000,
                     tolerance: float = 1e-12) -> MethodResult:
    if not obs:
        return MethodResult("bias", {})
    mu = statistics.fmean(o.score for o in obs)
    by_project = _by(obs, "project")
    by_judge = _by(obs, "judge")
    a = {p: 0.0 for p in by_project}
    b = {j: 0.0 for j in by_judge}
    steps = 0
    for steps in range(1, iterations + 1):
        change = 0.0
        for p, rows in by_project.items():
            value = statistics.fmean(o.score - mu - b[o.judge] for o in rows)
            change = max(change, abs(value - a[p]))
            a[p] = value
        for j, rows in by_judge.items():
            value = sum(o.score - mu - a[o.project] for o in rows) / (len(rows) + lam)
            change = max(change, abs(value - b[j]))
            b[j] = value
        if change < tolerance:
            break
    detail = {j: {"n": len(rows), "bias": b[j], "mean": statistics.fmean(o.score for o in rows)}
              for j, rows in by_judge.items()}
    residuals = [o.score - mu - a[o.project] - b[o.judge] for o in obs]
    rmse = math.sqrt(statistics.fmean(r * r for r in residuals))
    return MethodResult("bias", {p: mu + a[p] for p in a}, detail,
                        {"mu": mu, "lambda": lam, "iterations": steps, "residual_rmse": rmse})


def implied_comparisons(obs: list[Observation]) -> list[tuple[str, str, float]]:
    comparisons: list[tuple[str, str, float]] = []
    for rows in _by(obs, "judge").values():
        for x, y in combinations(rows, 2):
            if x.score > y.score:
                comparisons.append((x.project, y.project, 1.0))
            elif y.score > x.score:
                comparisons.append((y.project, x.project, 1.0))
            else:
                comparisons.append((x.project, y.project, 0.5))
                comparisons.append((y.project, x.project, 0.5))
    return comparisons


def within_judge_bt(obs: list[Observation]) -> MethodResult:
    projects = {o.project for o in obs}
    comparisons = implied_comparisons(obs)
    return MethodResult("pairwise", bradley_terry(comparisons, projects), info={"comparisons": len(comparisons)})


def compute(method: str, obs: list[Observation]) -> MethodResult:
    return {"raw": raw_mean, "zscore": zscore_shrunk, "bias": judge_bias_model, "pairwise": within_judge_bt}[method](obs)


def rank(scores: dict[str, float], tiebreak: dict[str, tuple] | None = None) -> dict[str, int]:
    """Competition ranking (1, 2, 2, 4). Exact score ties share a rank; the
    tiebreak tuple only orders display within a tie."""
    tiebreak = tiebreak or {}
    order = sorted(scores, key=lambda p: (-scores[p], tiebreak.get(p, ()), p))
    ranks: dict[str, int] = {}
    previous = None
    for position, project in enumerate(order, start=1):
        value = round(scores[project], 9)
        if previous is None or value != previous[0]:
            previous = (value, position)
        ranks[project] = previous[1]
    return ranks


def spearman(a: dict[str, float], b: dict[str, float]) -> float:
    """Rank correlation over the common keys (average ranks for ties)."""
    keys = sorted(set(a) & set(b))
    if len(keys) < 2:
        return 1.0

    def average_ranks(values: dict[str, float]) -> dict[str, float]:
        order = sorted(keys, key=lambda k: values[k])
        ranks: dict[str, float] = {}
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in order[i:j + 1]:
                ranks[k] = (i + j) / 2 + 1
            i = j + 1
        return ranks

    ra, rb = average_ranks(a), average_ranks(b)
    ma, mb = statistics.fmean(ra.values()), statistics.fmean(rb.values())
    cov = sum((ra[k] - ma) * (rb[k] - mb) for k in keys)
    sa = math.sqrt(sum((ra[k] - ma) ** 2 for k in keys))
    sb = math.sqrt(sum((rb[k] - mb) ** 2 for k in keys))
    return cov / (sa * sb) if sa and sb else 1.0


def judge_components(obs: list[Observation]) -> list[set[str]]:
    """Connected components of judges linked by shared projects. Within a
    component the bias model can compare judges; across components it can
    only fall back to raw levels."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rows in _by(obs, "project").values():
        judges = [o.judge for o in rows]
        for judge in judges:
            find(judge)
        for other in judges[1:]:
            parent[find(other)] = find(judges[0])
    groups: dict[str, set[str]] = defaultdict(set)
    for judge in list(parent):
        groups[find(judge)].add(judge)
    return sorted(groups.values(), key=len, reverse=True)
