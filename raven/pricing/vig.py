"""Vig (overround) removal for RAVEN's Fair-Value Engine (F2).

Bookmaker / consensus decimal odds embed an *overround* (a.k.a. vig, juice):
the raw implied probabilities sum to > 1. Before RAVEN can reason about a
"fair" price it must strip that margin so probabilities sum to 1.

Two methods are provided:

* :func:`multiplicative` — the fast, robust baseline used in the MVP. It scales
  every raw implied probability by ``1 / overround``. Simple, deterministic,
  and defensible; its known weakness is that it distributes vig proportionally,
  which slightly over-penalises longshots.

* :func:`shin` — Shin's (1992/1993) method, which models the overround as
  arising from a fraction ``z`` of insider ("informed") trading and solves for
  the fair probabilities that a book would set against it. It corrects the
  favourite-longshot bias the multiplicative method leaves behind. Documented
  here and wired in as the sharper option; see README for the derivation.

Both return a mapping ``outcome -> fair probability`` that sums to 1.0.
"""

from __future__ import annotations

from typing import Dict, Mapping


def _raw_implied(odds: Mapping[str, float]) -> Dict[str, float]:
    """Raw implied probabilities (1 / decimal odds); still contains vig."""
    return {k: (1.0 / v) for k, v in odds.items() if v and v > 0.0}


def overround(odds: Mapping[str, float]) -> float:
    """Booksum: the sum of raw implied probabilities (>= 1 when vig present)."""
    return sum(_raw_implied(odds).values())


def multiplicative(odds: Mapping[str, float]) -> Dict[str, float]:
    """Proportional (multiplicative) vig removal.

    ``p_fair_i = (1/odds_i) / sum_j (1/odds_j)``

    This is the RAVEN baseline: O(n), no iteration, always well-defined for
    positive odds.
    """
    raw = _raw_implied(odds)
    booksum = sum(raw.values())
    if booksum <= 0.0:
        return {}
    return {k: v / booksum for k, v in raw.items()}


def shin(
    odds: Mapping[str, float],
    *,
    max_iter: int = 100,
    tol: float = 1e-10,
) -> Dict[str, float]:
    """Shin's method for vig removal (favourite-longshot bias corrected).

    Shin models observed booksum as inflated by a proportion ``z`` of insider
    money. Given raw implied probabilities ``pi`` (which sum to the booksum),
    the fair probability of outcome ``i`` is

        p_i = ( sqrt( z^2 + 4 (1 - z) * pi_i^2 / booksum ) - z )
              / ( 2 (1 - z) )

    with ``z`` chosen so that ``sum_i p_i = 1``. We solve for ``z`` by a
    bounded bisection on ``[0, 1)``; ``z = 0`` reduces to the multiplicative
    result, so this method strictly generalises the baseline.

    Falls back to :func:`multiplicative` for degenerate (two-way or malformed)
    inputs where Shin is not well-conditioned.
    """
    raw = _raw_implied(odds)
    booksum = sum(raw.values())
    n = len(raw)
    if n < 2 or booksum <= 0.0:
        return multiplicative(odds)

    keys = list(raw.keys())
    pi = [raw[k] for k in keys]

    def fair_given_z(z: float) -> list[float]:
        out = []
        denom = 2.0 * (1.0 - z)
        for p in pi:
            inside = z * z + 4.0 * (1.0 - z) * (p * p) / booksum
            inside = max(inside, 0.0)
            out.append((inside**0.5 - z) / denom)
        return out

    def sum_fair(z: float) -> float:
        return sum(fair_given_z(z))

    # sum_fair is monotonically decreasing in z on [0, 1); at z->0 it equals
    # booksum (>= 1), and it decreases below 1 as z grows. Bisect for sum == 1.
    lo, hi = 0.0, 0.999_999
    s_lo = sum_fair(lo)
    if s_lo <= 1.0:
        # No vig to remove (or already fair): use multiplicative normalisation.
        return multiplicative(odds)

    z = 0.0
    for _ in range(max_iter):
        z = 0.5 * (lo + hi)
        s = sum_fair(z)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = z
        else:
            hi = z

    fair = fair_given_z(z)
    total = sum(fair)
    if total <= 0.0:
        return multiplicative(odds)
    # Renormalise to kill any residual bisection error.
    return {k: f / total for k, f in zip(keys, fair)}


def remove_vig(odds: Mapping[str, float], method: str = "multiplicative") -> Dict[str, float]:
    """Dispatch to the configured vig-removal method."""
    if method == "shin":
        return shin(odds)
    return multiplicative(odds)
