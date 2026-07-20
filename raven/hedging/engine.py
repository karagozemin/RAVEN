"""Self-Hedging Engine (F6).

RAVEN quotes several *connected* markets on the same match at once — Match
Winner (1X2), Total Goals (over/under) and Asian Handicap. Those markets are not
independent: a single home goal moves all of them together. So RAVEN never looks
at one position in isolation. It rolls every open position into **one portfolio**
and asks a sharper question:

    "If the next thing that happens is a home goal (or an away goal, or a red
    card, or nothing at all until settlement), how much do I lose?"

Each of those *shocks* is a scenario. For every scenario we revalue the whole
book and record the P&L swing — that is the :class:`ShockExposure`. The worst
(most negative) scenario is RAVEN's real risk, no matter how flat any single
market looks.

The engine then chooses a **concrete cross-market hedge** (FR6.4): a trade in a
*different* connected market whose payoff under the worst shock offsets the
book's. For example a book that is dangerously long "home goals" can be
neutralized by selling Over in Total Goals, or by taking the away side of the
handicap — whichever reduces the worst-case loss most per unit of spread given
up (FR6.3). It deliberately does **not** hedge all the way to zero: killing the
last sliver of exposure usually costs more spread than it saves.

Everything here is deterministic and side-effect free. :meth:`HedgeEngine.plan`
reads the inventory and returns a :class:`HedgePlan`; applying that plan (and the
resulting fills) is the caller's job, so the same book always yields the same
plan for audit and replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from raven.quoting.inventory import Inventory


class Shock(str, Enum):
    """The discrete match events RAVEN stress-tests its book against.

    These are the state transitions that move *all* connected football markets
    at once. ``NO_MORE_GOALS`` is the "nothing happens" scenario — the match
    settles at the current score — which is the one that actually pays a market
    maker who is short goals, so it must be modelled too.
    """

    HOME_GOAL = "home_goal"
    AWAY_GOAL = "away_goal"
    RED_CARD_HOME = "red_card_home"
    NO_MORE_GOALS = "no_more_goals"


@dataclass(frozen=True)
class ShockExposure:
    """The portfolio P&L swing under one :class:`Shock`.

    ``delta`` is signed in account currency (or notional units): negative means
    RAVEN *loses* if this shock occurs. ``contributions`` breaks the swing down
    by ``(market, outcome)`` so the Decision Inspector and receipts can explain
    exactly which positions drive the risk.
    """

    shock: Shock
    delta: float
    contributions: Dict[Tuple[str, str], float] = field(default_factory=dict)

    @property
    def is_loss(self) -> bool:
        return self.delta < 0.0


@dataclass(frozen=True)
class HedgeTrade:
    """One concrete hedge order in a connected market (FR6.4).

    ``side`` is ``"buy"`` (go long the outcome) or ``"sell"`` (go short).
    ``price`` is the probability-space price RAVEN expects to trade at; it is
    used only to estimate the spread/cost given up, never to move a quote.
    """

    market: str
    outcome: str
    side: str
    size: float
    price: float

    def signed_quantity(self) -> float:
        return self.size if self.side == "buy" else -self.size


@dataclass(frozen=True)
class HedgePlan:
    """The hedge the engine recommends, plus the risk picture behind it.

    ``worst_before`` / ``worst_after`` are the most-negative shock deltas across
    all scenarios before and after applying ``trades`` — the headline the
    counterfactual lab and demo quote ("+12,480 -> +930"). ``residual`` is the
    signed worst-case exposure that remains on purpose (FR6.3).
    """

    trades: List[HedgeTrade]
    worst_before: ShockExposure
    worst_after: ShockExposure
    residual: float
    exposures_before: Dict[Shock, ShockExposure]
    exposures_after: Dict[Shock, ShockExposure]

    @property
    def is_noop(self) -> bool:
        return not self.trades

    @property
    def reduction(self) -> float:
        """How much worst-case loss the hedge removed (>= 0)."""
        return abs(self.worst_before.delta) - abs(self.worst_after.delta)


# --- payoff model ----------------------------------------------------------
#
# For every (market, outcome) we need to know what a unit long position is worth
# *after* each shock, expressed as a probability in 0..1 (the contract settles at
# 1.0 if the outcome ends up true). We model this as a shift from the outcome's
# current average price: a home goal makes "home win"/"over" more likely, an away
# goal the reverse, and so on. The shifts are intentionally simple, bounded and
# monotonic — enough to rank hedges correctly, and easy to defend to a quant.

# Per-outcome probability shift caused by each shock, by market family.
# Keys are canonical outcome names; the handicap map keys on the favoured side.
_SHIFTS: Dict[str, Dict[Shock, float]] = {
    # Match Winner (1X2)
    "home": {
        Shock.HOME_GOAL: +0.28,
        Shock.AWAY_GOAL: -0.24,
        Shock.RED_CARD_HOME: -0.18,
        Shock.NO_MORE_GOALS: +0.05,
    },
    "draw": {
        Shock.HOME_GOAL: -0.14,
        Shock.AWAY_GOAL: -0.14,
        Shock.RED_CARD_HOME: +0.04,
        Shock.NO_MORE_GOALS: +0.10,
    },
    "away": {
        Shock.HOME_GOAL: -0.24,
        Shock.AWAY_GOAL: +0.28,
        Shock.RED_CARD_HOME: +0.16,
        Shock.NO_MORE_GOALS: -0.05,
    },
    # Total Goals
    "over": {
        Shock.HOME_GOAL: +0.30,
        Shock.AWAY_GOAL: +0.30,
        Shock.RED_CARD_HOME: -0.06,
        Shock.NO_MORE_GOALS: -0.25,
    },
    "under": {
        Shock.HOME_GOAL: -0.30,
        Shock.AWAY_GOAL: -0.30,
        Shock.RED_CARD_HOME: +0.06,
        Shock.NO_MORE_GOALS: +0.25,
    },
    # Asian Handicap — favoured (home) side and underdog (away) side.
    "home_ah": {
        Shock.HOME_GOAL: +0.26,
        Shock.AWAY_GOAL: -0.26,
        Shock.RED_CARD_HOME: -0.18,
        Shock.NO_MORE_GOALS: +0.03,
    },
    "away_ah": {
        Shock.HOME_GOAL: -0.26,
        Shock.AWAY_GOAL: +0.26,
        Shock.RED_CARD_HOME: +0.18,
        Shock.NO_MORE_GOALS: -0.03,
    },
}

# Map raw outcome names onto the canonical shift keys above.
_ALIASES: Dict[str, str] = {
    "1": "home",
    "x": "draw",
    "2": "away",
    "home_win": "home",
    "away_win": "away",
    "over_2.5": "over",
    "under_2.5": "under",
    "o": "over",
    "u": "under",
    "home_handicap": "home_ah",
    "away_handicap": "away_ah",
    "home -0.5": "home_ah",
    "away +0.5": "away_ah",
}


def _canonical(market: str, outcome: str) -> Optional[str]:
    key = str(outcome).strip().lower()
    market_key = str(market).strip().lower()
    if "handicap" in market_key and key in {"home", "away"}:
        return f"{key}_ah"
    if "total" in market_key and key in {"over", "under"}:
        return key
    if key in _SHIFTS:
        return key
    return _ALIASES.get(key)


def _shift(market: str, outcome: str, shock: Shock) -> float:
    canon = _canonical(market, outcome)
    if canon is None:
        return 0.0
    return _SHIFTS[canon].get(shock, 0.0)


class HedgeEngine:
    """Computes cross-market shock exposure and a neutralizing hedge.

    Parameters
    ----------
    hedge_universe:
        The ``(market, outcome, price)`` instruments RAVEN is willing to trade
        as hedges, with the price it expects to pay/receive (probability units).
        These are *connected* markets other than the one carrying the risk, so
        the hedge is genuinely cross-market (FR6.4).
    max_hedge_size:
        Hard cap on the size of any single hedge trade.
    residual_target:
        Fraction of the original worst-case loss RAVEN is content to leave
        unhedged (FR6.3). ``0.1`` means "hedge until ~90% of the worst shock is
        removed, then stop" — the remaining sliver is cheaper to carry than to
        close.
    cost_weight:
        Penalty per unit of spread/cost given up, traded off against risk
        removed when ranking candidate hedges. Higher = more cost-averse.
    """

    def __init__(
        self,
        *,
        hedge_universe: Optional[List[Tuple[str, str, float]]] = None,
        max_hedge_size: float = 1000.0,
        residual_target: float = 0.1,
        cost_weight: float = 0.15,
    ) -> None:
        if max_hedge_size <= 0.0:
            raise ValueError("max_hedge_size must be positive")
        if not 0.0 <= residual_target < 1.0:
            raise ValueError("residual_target must be in [0, 1)")
        self.hedge_universe = hedge_universe or []
        self.max_hedge_size = max_hedge_size
        self.residual_target = residual_target
        self.cost_weight = cost_weight

    # -- exposure -----------------------------------------------------------

    def exposures(self, inventory: Inventory) -> Dict[Shock, ShockExposure]:
        """Portfolio P&L swing under every modelled shock.

        For each shock we revalue every open position: a long ``quantity`` of an
        outcome gains ``quantity * shift`` when the shock moves that outcome's
        settlement probability by ``shift``. Summing across the whole book gives
        the portfolio delta, which is exactly the cross-market netting the naive
        per-market view misses.
        """
        result: Dict[Shock, ShockExposure] = {}
        positions = list(inventory)
        for shock in Shock:
            total = 0.0
            contributions: Dict[Tuple[str, str], float] = {}
            for pos in positions:
                d = pos.quantity * _shift(pos.market, pos.outcome, shock)
                if d != 0.0:
                    contributions[(pos.market, pos.outcome)] = round(d, 6)
                    total += d
            result[shock] = ShockExposure(
                shock=shock,
                delta=round(total, 6),
                contributions=contributions,
            )
        return result

    def worst(self, exposures: Dict[Shock, ShockExposure]) -> ShockExposure:
        """The most-negative (worst-case) shock exposure."""
        return min(exposures.values(), key=lambda e: e.delta)

    # -- hedge selection ----------------------------------------------------

    def plan(self, inventory: Inventory) -> HedgePlan:
        """Greedily pick cross-market hedges that shrink the worst-case loss.

        The loop is deliberately simple and auditable: while the current
        worst-case shock is still a meaningful loss, evaluate every instrument
        in the hedge universe, score each by *risk removed minus cost given up*,
        and apply the best one at the size that neutralizes the current worst
        shock (capped by ``max_hedge_size``). Stop once the residual worst case
        is within ``residual_target`` of where it started, or nothing helps.
        """
        exposures_before = self.exposures(inventory)
        worst_before = self.worst(exposures_before)

        # A book that isn't losing under any shock needs no hedge.
        if worst_before.delta >= 0.0 or not self.hedge_universe:
            return HedgePlan(
                trades=[],
                worst_before=worst_before,
                worst_after=worst_before,
                residual=worst_before.delta,
                exposures_before=exposures_before,
                exposures_after=exposures_before,
            )

        # Work on a scratch book so the caller's inventory is untouched.
        work = _clone(inventory)
        trades: List[HedgeTrade] = []
        stop_at = abs(worst_before.delta) * self.residual_target

        # Bounded number of passes keeps this deterministic and fast.
        for _ in range(len(self.hedge_universe) * 2):
            exposures = self.exposures(work)
            worst = self.worst(exposures)
            if worst.delta >= -stop_at:
                break

            best = self._best_trade(work, worst)
            if best is None:
                break

            work.apply_fill(
                best.market, best.outcome, best.signed_quantity(), best.price
            )
            trades.append(best)

        exposures_after = self.exposures(work)
        worst_after = self.worst(exposures_after)
        return HedgePlan(
            trades=_merge(trades),
            worst_before=worst_before,
            worst_after=worst_after,
            residual=worst_after.delta,
            exposures_before=exposures_before,
            exposures_after=exposures_after,
        )

    def _best_trade(
        self, work: Inventory, worst: ShockExposure
    ) -> Optional[HedgeTrade]:
        """Pick the instrument that best offsets the current worst shock.

        For each candidate we work out the direction (buy/sell) that *gains*
        under the worst shock, the size that would neutralize it, and a score of
        ``risk_removed - cost_weight * cost``. The highest positive score wins.
        """
        best: Optional[HedgeTrade] = None
        best_score = 0.0

        current_loss = max(0.0, -worst.delta)
        for market, outcome, price in self.hedge_universe:
            shift = _shift(market, outcome, worst.shock)
            if shift == 0.0:
                continue

            # We want a position that gains under the worst shock. A long gains
            # when shift > 0; a short gains when shift < 0. Either way, the size
            # that offsets the loss is |delta| / |shift|.
            target_size = min(
                abs(worst.delta) / abs(shift), self.max_hedge_size
            )
            side = "buy" if shift > 0.0 else "sell"

            # A hedge aimed at the current worst scenario can create a larger
            # loss in another scenario. Test several deterministic sizes and
            # rank them by the *new portfolio worst case*, not local delta.
            for fraction in (1.0, 0.5, 0.25, 0.1):
                size = target_size * fraction
                if size <= 0.0:
                    continue
                candidate = HedgeTrade(
                    market=market,
                    outcome=outcome,
                    side=side,
                    size=round(size, 4),
                    price=price,
                )
                trial = _clone(work)
                trial.apply_fill(
                    market,
                    outcome,
                    candidate.signed_quantity(),
                    price,
                )
                new_worst = self.worst(self.exposures(trial))
                risk_removed = current_loss - max(0.0, -new_worst.delta)
                if risk_removed <= 0.0:
                    continue
                cost = size * _round_trip_cost(price)
                score = risk_removed - self.cost_weight * cost
                if score > best_score:
                    best_score = score
                    best = candidate

        return best


# --- small helpers ---------------------------------------------------------


def _round_trip_cost(price: float) -> float:
    """Rough cost of crossing the spread to put on a hedge at ``price``.

    Extremes (prices near 0 or 1) are wider in probability terms, so we make the
    cost mildly convex around the mid. This only *ranks* hedges, so an
    approximation is fine and defensible.
    """
    p = min(max(price, 1e-6), 1.0 - 1e-6)
    return 0.01 + 0.04 * (1.0 - abs(0.5 - p) * 2.0)


def _clone(inventory: Inventory) -> Inventory:
    """Deep-ish copy of the book so hedge search never mutates live state."""
    clone = Inventory()
    for pos in inventory:
        if not pos.is_flat:
            clone.apply_fill(pos.market, pos.outcome, pos.quantity, pos.avg_price)
    return clone


def _merge(trades: List[HedgeTrade]) -> List[HedgeTrade]:
    """Net repeated and opposing trades into one line per instrument."""
    acc: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
    order: List[Tuple[str, str]] = []
    for t in trades:
        k = (t.market, t.outcome)
        signed = t.signed_quantity()
        if k not in acc:
            order.append(k)
            acc[k] = (0.0, 0.0, 0.0)
        net, weighted_price, gross = acc[k]
        acc[k] = (
            net + signed,
            weighted_price + t.size * t.price,
            gross + t.size,
        )

    merged: List[HedgeTrade] = []
    for market, outcome in order:
        net, weighted_price, gross = acc[(market, outcome)]
        if abs(net) < 1e-6:
            continue
        merged.append(
            HedgeTrade(
                market=market,
                outcome=outcome,
                side="buy" if net > 0 else "sell",
                size=round(abs(net), 4),
                price=round(weighted_price / gross, 6),
            )
        )
    return merged
