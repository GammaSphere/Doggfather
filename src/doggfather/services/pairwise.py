"""Bradley-Terry estimation (pure functions, no database).

Model: P(i beats j) = p_i / (p_i + p_j), with strengths p_i > 0.

We fit by the minorize-maximize iteration of Hunter (2004), "MM algorithms
for generalized Bradley-Terry models", Annals of Statistics 32(1). To keep
sparse data well-posed (an item that never lost would otherwise run off to
infinity), every item also plays ``prior`` virtual wins and ``prior``
virtual losses against a fixed reference item of strength 1. That is the
same as a weak symmetric prior centred on "average". The update is

    p_i <- (W_i + prior) / ( sum_j n_ij / (p_i + p_j) + 2*prior / (p_i + 1) )

where W_i is i's (weighted) win count and n_ij the (weighted) number of
comparisons between i and j. Ties count as half a win each way.

Returned values are log-strengths centred on zero, so +0.7 means "about
twice as likely as an average project to be preferred".
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

Comparison = tuple[str, str, float]  # (winner, loser, weight)


def bradley_terry(comparisons: Iterable[Comparison], items: Iterable[str] | None = None, *,
                  prior: float = 0.5, iterations: int = 2000, tolerance: float = 1e-10) -> dict[str, float]:
    wins: dict[str, float] = defaultdict(float)
    games: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: set[str] = set(items or [])
    for winner, loser, weight in comparisons:
        if winner == loser or weight <= 0:
            continue
        names.update((winner, loser))
        wins[winner] += weight
        games[winner][loser] += weight
        games[loser][winner] += weight
    if not names:
        return {}

    strength = {name: 1.0 for name in names}
    for _ in range(iterations):
        biggest_step = 0.0
        updated = {}
        for i in names:
            denominator = 2 * prior / (strength[i] + 1.0)
            for j, n_ij in games[i].items():
                denominator += n_ij / (strength[i] + strength[j])
            value = (wins[i] + prior) / denominator if denominator > 0 else 1.0
            updated[i] = value
            biggest_step = max(biggest_step, abs(math.log(value) - math.log(strength[i])))
        strength = updated
        if biggest_step < tolerance:
            break

    logs = {name: math.log(value) for name, value in strength.items()}
    centre = sum(logs.values()) / len(logs)
    return {name: value - centre for name, value in logs.items()}


def win_probability(theta_a: float, theta_b: float) -> float:
    return 1.0 / (1.0 + math.exp(theta_b - theta_a))
