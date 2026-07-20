"""Event-hazard scoring model for RAVEN's Fair-Value Engine (F2).

RAVEN prices the *remaining* match, not the match from kick-off. Given the
current :class:`~raven.pricing.state.MatchState` (minutes remaining, current
score, red cards) we model additional goals for each side over the rest of
regulation time as independent (inhomogeneous) Poisson processes, then build
the full remaining-goals distribution.

Why this framing (and NOT "theta decay"):
    Football scoring is not a smooth, one-directional time decay. The
    probability of each outcome evolves as a function of *remaining scoring
    opportunity* (an integrated hazard) and the *current score*. A 0-0 with 5
    minutes left is nearly a lock for "few goals"; the same 0-0 at minute 10 is
    wide open. We therefore model the goal *hazard* over remaining time, which
    is the statistically honest object, and derive outcome probabilities from
    it.

Design constraints:
    * Deterministic: identical state -> identical distribution, always.
    * Bounded: goal counts are truncated at :data:`MAX_GOALS` per side, which
      captures essentially all probability mass for football.
    * Cheap: closed-form Poisson pmf, O(MAX_GOALS^2) convolution per pricing
      call — microseconds, safe to run on every frame.

The absolute intensity level is intentionally kept modest and is *anchored to
the market* downstream (see :mod:`raven.pricing.fair_value`): the hazard model
supplies the SHAPE of the remaining-goals distribution and its response to
state changes, while TxLINE consensus supplies the LEVEL. This keeps model
risk bounded — we never let the model wander far from the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

from raven.pricing.state import MatchState

# Truncation for the per-side goal distribution. P(>=8 more goals for one side
# in the remainder of a match) is vanishingly small; 8 is a safe cap.
MAX_GOALS = 8

# Baseline expected goals per team across a FULL regulation match. This is the
# model's only free "level" parameter and is deliberately league-agnostic; the
# fair-value layer rescales the resulting distribution to the market anyway.
BASE_LAMBDA_PER_TEAM = 1.35

# Multiplicative bump to a team's remaining intensity when it has a one-man
# advantage (opponent has one more red card). Empirically a sending-off is
# worth roughly this much to the team with more men.
RED_CARD_ATTACK_BONUS = 1.25
RED_CARD_DEFENCE_PENALTY = 0.80


def _poisson_pmf(lmbda: float, k: int) -> float:
    """Poisson probability mass P(X = k) for rate ``lmbda``."""
    if lmbda <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lmbda + k * math.log(lmbda) - math.lgamma(k + 1))


def _truncated_pmf_vector(lmbda: float) -> List[float]:
    """Poisson pmf over 0..MAX_GOALS, renormalised to sum to 1."""
    vec = [_poisson_pmf(lmbda, k) for k in range(MAX_GOALS + 1)]
    total = sum(vec)
    if total <= 0.0:
        out = [0.0] * (MAX_GOALS + 1)
        out[0] = 1.0
        return out
    return [v / total for v in vec]


@dataclass(frozen=True)
class RemainingGoals:
    """Joint distribution of *additional* goals for (home, away) over the rest.

    ``joint[i][j]`` is P(home scores i more, away scores j more) for
    ``i, j in [0, MAX_GOALS]``, assuming independence between the two sides'
    remaining goal counts.
    """

    home_lambda: float
    away_lambda: float
    joint: List[List[float]]

    @classmethod
    def from_lambdas(cls, home_lambda: float, away_lambda: float) -> "RemainingGoals":
        home_pmf = _truncated_pmf_vector(home_lambda)
        away_pmf = _truncated_pmf_vector(away_lambda)
        joint = [[hp * ap for ap in away_pmf] for hp in home_pmf]
        return cls(home_lambda=home_lambda, away_lambda=away_lambda, joint=joint)


def remaining_intensities(state: MatchState) -> tuple[float, float]:
    """Expected *remaining* goals (lambda) for (home, away) given match state.

    The full-match baseline is scaled by the fraction of regulation time still
    to play, then adjusted for man-advantage from red cards.
    """
    frac = state.fraction_remaining
    home = BASE_LAMBDA_PER_TEAM * frac
    away = BASE_LAMBDA_PER_TEAM * frac

    # Red-card man-advantage: the side with fewer reds attacks more, defends
    # about the same; the short-handed side is penalised on both ends.
    net_reds = state.red_away - state.red_home  # >0 => home has advantage
    if net_reds > 0:
        home *= RED_CARD_ATTACK_BONUS ** net_reds
        away *= RED_CARD_DEFENCE_PENALTY ** net_reds
    elif net_reds < 0:
        n = -net_reds
        away *= RED_CARD_ATTACK_BONUS ** n
        home *= RED_CARD_DEFENCE_PENALTY ** n

    return home, away


def remaining_goals(state: MatchState) -> RemainingGoals:
    """Build the remaining-goals joint distribution for a match state."""
    home_lambda, away_lambda = remaining_intensities(state)
    return RemainingGoals.from_lambdas(home_lambda, away_lambda)


def match_winner_probs(state: MatchState, rg: RemainingGoals) -> Dict[str, float]:
    """P(home / draw / away) for the FINAL result given current score.

    Combines the current goal difference with the remaining-goals joint:
    the final margin is ``goal_diff + (i - j)``.
    """
    gd = state.goal_difference
    p_home = p_draw = p_away = 0.0
    for i, row in enumerate(rg.joint):
        for j, p in enumerate(row):
            final_margin = gd + (i - j)
            if final_margin > 0:
                p_home += p
            elif final_margin == 0:
                p_draw += p
            else:
                p_away += p
    return {"home": p_home, "draw": p_draw, "away": p_away}


def total_goals_probs(state: MatchState, rg: RemainingGoals, line: float) -> Dict[str, float]:
    """P(over / under) total match goals for an Over/Under ``line`` (e.g. 2.5).

    Total final goals = current total + (i + j). Lines are typically x.5 so no
    push; if an integer line is passed, exact-total mass is split out as the
    remaining probability (neither over nor under).
    """
    current_total = state.score.home + state.score.away
    p_over = p_under = 0.0
    for i, row in enumerate(rg.joint):
        for j, p in enumerate(row):
            final_total = current_total + i + j
            if final_total > line:
                p_over += p
            elif final_total < line:
                p_under += p
            # exact == line (integer lines) is a push: excluded from both
    return {"over": p_over, "under": p_under}


def asian_handicap_probs(
    state: MatchState, rg: RemainingGoals, handicap: float
) -> Dict[str, float]:
    """P(home covers / away covers) for a home ``handicap`` (e.g. -0.5, +1.0).

    The home side "covers" when ``final_margin + handicap > 0``. Quarter/whole
    lines that can push are handled by excluding exact-zero adjusted margins
    (half-line handicaps never push).
    """
    gd = state.goal_difference
    p_home = p_away = 0.0
    for i, row in enumerate(rg.joint):
        for j, p in enumerate(row):
            adj = (gd + (i - j)) + handicap
            if adj > 0:
                p_home += p
            elif adj < 0:
                p_away += p
            # adj == 0 (integer handicap) pushes: stake returned, excluded
    return {"home": p_home, "away": p_away}
