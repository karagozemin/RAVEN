"""Fair-Value Engine for RAVEN (F2).

This module is the composition layer of the Fair-Value Engine. It fuses two
independent sources of truth into a single, defensible fair probability per
outcome:

* **LEVEL — TxLINE consensus.** The vig-removed consensus odds
  (:mod:`raven.pricing.vig`) tell us where the *market* is right now. This is
  our anchor: the crowd of sharp books is, on average, hard to beat on level.

* **SHAPE — event-hazard model.** The remaining-goals distribution
  (:mod:`raven.pricing.hazard`) tells us how outcome probabilities *respond* to
  match state — score, minutes remaining, red cards. This is what lets RAVEN
  react to a goal or sending-off *before* every connected market has caught up.

The two are blended, and then a **model-risk cap** (FR2.4) constrains the
result so it can never deviate from the consensus by more than a bounded
amount. This is the single most important safety property of the pricing stack:

    "The hazard model supplies the shape of the move and its speed; TxLINE
    supplies the level. We never let the model wander far from the market."

The output is a :class:`FairValue` per market, which the Quote Engine (F3) turns
into a reservation price and a two-sided quote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from raven.feed.model import OddsSnapshot
from raven.pricing import hazard
from raven.pricing.state import MatchState
from raven.pricing.vig import remove_vig

# --- Market classification -------------------------------------------------

# Numeric parameter embedded in a market label, e.g. "total_goals@2.5",
# "asian_handicap@-0.5", "totals 2.5", "ah:-1". We accept a signed decimal
# anywhere after a separator so the engine is forgiving about label formats.
_LINE_RE = re.compile(r"[@:_\s]([+-]?\d+(?:\.\d+)?)\s*$")

_MATCH_WINNER_KEYS = frozenset({"home", "draw", "away"})
_TOTALS_KEYS = frozenset({"over", "under"})
_HANDICAP_KEYS = frozenset({"home", "away"})


def extract_line(market: str) -> Optional[float]:
    """Pull a trailing numeric line/handicap out of a market label.

    Returns ``None`` when no numeric parameter is present (e.g. a plain
    "match_winner"). Defensive: never raises on odd labels.
    """
    if not market:
        return None
    m = _LINE_RE.search(str(market))
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FairValue:
    """Fair probabilities for one market, with full provenance for audit.

    ``probabilities`` is the blended, model-risk-capped result the rest of the
    system consumes. ``market_probs`` and ``model_probs`` are retained so a
    decision receipt (F7) or the Decision Inspector (F11) can explain *why* the
    fair value sits where it does.
    """

    market: str
    probabilities: Dict[str, float]
    market_probs: Dict[str, float]
    model_probs: Optional[Dict[str, float]]
    model_weight: float
    cap_binding: bool
    line: Optional[float] = None

    def fair_odds(self) -> Dict[str, float]:
        """Fair decimal odds (1 / probability) for each outcome."""
        return {
            k: (1.0 / p) if p > 0.0 else float("inf")
            for k, p in self.probabilities.items()
        }


class FairValueEngine:
    """Blends consensus level with hazard-model shape under a model-risk cap.

    Parameters
    ----------
    model_weight:
        Baseline weight on the hazard model in the blend, in ``[0, 1]``. The
        market carries ``1 - model_weight``. Kept modest (the market is the
        anchor); the model's real job is to move *fast* on state changes, and
        the cap below guarantees it can never move *far*.
    max_deviation:
        The model-risk cap (FR2.4). No blended probability may sit more than
        this far (in absolute probability) from the consensus probability.
    vig_method:
        ``"multiplicative"`` (MVP baseline) or ``"shin"`` (favourite-longshot
        corrected).
    """

    def __init__(
        self,
        *,
        model_weight: float = 0.35,
        max_deviation: float = 0.08,
        vig_method: str = "multiplicative",
    ) -> None:
        if not 0.0 <= model_weight <= 1.0:
            raise ValueError("model_weight must be in [0, 1]")
        if not 0.0 <= max_deviation <= 1.0:
            raise ValueError("max_deviation must be in [0, 1]")
        self.model_weight = model_weight
        self.max_deviation = max_deviation
        self.vig_method = vig_method

    # -- public API ---------------------------------------------------------

    def price(self, odds: OddsSnapshot, state: MatchState) -> FairValue:
        """Compute the fair value for one market given the current match state.

        The pipeline is: vig removal -> model shape (if the market is one we
        model) -> weighted blend -> model-risk cap -> renormalise.
        """
        market_probs = self._normalise_keys(remove_vig(odds.outcomes, self.vig_method))
        line = extract_line(odds.market)
        model_probs = self._model_probs(odds, state, line)

        if not market_probs:
            # Degenerate consensus (no positive odds): nothing to anchor to.
            return FairValue(
                market=odds.market,
                probabilities={},
                market_probs={},
                model_probs=model_probs,
                model_weight=self.model_weight,
                cap_binding=False,
                line=line,
            )

        if model_probs is None:
            # Unmodelled market: pass the consensus through untouched.
            return FairValue(
                market=odds.market,
                probabilities=dict(market_probs),
                market_probs=market_probs,
                model_probs=None,
                model_weight=0.0,
                cap_binding=False,
                line=line,
            )

        blended, cap_binding = self._blend_and_cap(market_probs, model_probs)
        return FairValue(
            market=odds.market,
            probabilities=blended,
            market_probs=market_probs,
            model_probs=model_probs,
            model_weight=self.model_weight,
            cap_binding=cap_binding,
            line=line,
        )

    # -- internals ----------------------------------------------------------

    def _model_probs(
        self, odds: OddsSnapshot, state: MatchState, line: Optional[float]
    ) -> Optional[Dict[str, float]]:
        """Produce hazard-model probabilities aligned to the market's outcomes.

        Returns ``None`` if the market is not one RAVEN models (in which case
        the consensus passes through unmodified).
        """
        keys = {k.strip().lower() for k in odds.outcomes.keys()}
        rg = hazard.remaining_goals(state)

        if keys == _MATCH_WINNER_KEYS:
            return hazard.match_winner_probs(state, rg)

        if keys == _TOTALS_KEYS:
            if line is None:
                return None
            return hazard.total_goals_probs(state, rg, line)

        # Asian handicap and match-winner-without-draw share {home, away}; the
        # presence of a numeric line disambiguates a handicap market.
        if keys == _HANDICAP_KEYS and line is not None:
            return hazard.asian_handicap_probs(state, rg, line)

        return None

    def _blend_and_cap(
        self, market_probs: Dict[str, float], model_probs: Dict[str, float]
    ) -> tuple[Dict[str, float], bool]:
        """Weighted blend of market and model, clamped to the model-risk cap.

        Steps:
          1. ``p = (1 - w) * market + w * model`` per outcome.
          2. Clamp each ``p`` to ``[market - cap, market + cap]`` (FR2.4).
          3. Renormalise to sum to 1 so the result is a valid distribution.
        """
        w = self.model_weight
        cap = self.max_deviation
        cap_binding = False

        clamped: Dict[str, float] = {}
        for k, pm in market_probs.items():
            pmodel = model_probs.get(k, pm)
            blended = (1.0 - w) * pm + w * pmodel
            lo, hi = pm - cap, pm + cap
            if blended < lo:
                blended = lo
                cap_binding = True
            elif blended > hi:
                blended = hi
                cap_binding = True
            clamped[k] = max(0.0, blended)

        total = sum(clamped.values())
        if total <= 0.0:
            return dict(market_probs), cap_binding
        return {k: v / total for k, v in clamped.items()}, cap_binding

    @staticmethod
    def _normalise_keys(probs: Dict[str, float]) -> Dict[str, float]:
        """Lower-case / strip outcome keys so alignment is robust."""
        return {str(k).strip().lower(): v for k, v in probs.items()}
