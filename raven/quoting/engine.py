"""Quote Engine for RAVEN (F3).

This is the layer that turns a *fair value* (what RAVEN believes an outcome is
worth, from :mod:`raven.pricing.fair_value`) into an actual **two-sided quote**
it is willing to show the market: a bid it will buy at, an ask it will sell at,
and the size and lifetime of each.

Three ideas drive every quote:

* **Reservation price.** The centre of the quote is the fair probability, not
  the raw market mid. RAVEN quotes around what it believes, anchored (via the
  Fair-Value Engine's model-risk cap) to the consensus.

* **Inventory skew (FR3.2).** If RAVEN is already long an outcome it shades the
  whole quote *down* — cheaper to sell to it, dearer to buy from it — so flow
  naturally flattens the book instead of piling on more of the same risk. The
  skew is read from :class:`~raven.quoting.inventory.Inventory`.

* **Defensive spread (FR3.3).** The half-spread starts at a base and widens
  additively with every source of adverse-selection risk we can measure:
  event hazard, feed latency, realised volatility, inventory imbalance, and
  cross-market incoherence. When any of these spikes, RAVEN quotes wider (or,
  at the Risk Kernel's request, withdraws entirely). Fractional Kelly caps the
  *size*, never the price (FR3.4).

The engine itself is deterministic and side-effect free: the same fair value,
inventory and risk inputs always produce the same :class:`QuoteSet`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from raven.pricing.fair_value import FairValue
from raven.quoting.inventory import Inventory


@dataclass(frozen=True)
class Quote:
    """A single two-sided quote on one market outcome.

    Prices are in probability space (``0..1``). ``bid``/``ask`` are the prices
    RAVEN will buy/sell at; ``bid_size``/``ask_size`` are the sizes it will show
    on each side. When ``withdrawn`` is ``True`` the engine is standing aside
    (both sizes are zero) and ``reason`` explains why — this is what the Risk
    Kernel (F4) drives during a toxic-event window.
    """

    market: str
    outcome: str
    fair: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    expiry_ms: int
    withdrawn: bool = False
    reason: Optional[str] = None

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class QuoteSet:
    """All quotes RAVEN is showing on a single market at one instant."""

    market: str
    quotes: Dict[str, Quote]
    half_spread: float
    withdrawn: bool = False

    def outcome(self, name: str) -> Optional[Quote]:
        return self.quotes.get(str(name).strip().lower())

    def is_two_sided(self) -> bool:
        """True when at least one outcome is actively quoting on both sides."""
        return any(
            (not q.withdrawn) and q.bid_size > 0.0 and q.ask_size > 0.0
            for q in self.quotes.values()
        )


@dataclass(frozen=True)
class SpreadInputs:
    """Normalized risk signals that widen the spread (FR3.3).

    Every field is a dimensionless ``>= 0`` magnitude supplied by the caller
    (the main loop / Risk Kernel). Keeping them explicit — rather than reaching
    into match state here — keeps the engine deterministic and unit-testable,
    and lets F4/F9 decide how each signal is measured.

    * ``event_hazard`` — remaining-goal / sending-off pressure from the hazard
      model; high right after a goal or during a dangerous phase.
    * ``latency`` — staleness of the TxLINE feed (how long since a verified
      update relative to the tolerated budget).
    * ``volatility`` — realised jumpiness of the consensus for this market.
    * ``incoherence`` — Market Dependency Graph signal (F5): linked markets have
      not yet repriced consistently with this one.
    """

    event_hazard: float = 0.0
    latency: float = 0.0
    volatility: float = 0.0
    incoherence: float = 0.0

    def clamp(self) -> "SpreadInputs":
        return SpreadInputs(
            event_hazard=max(0.0, self.event_hazard),
            latency=max(0.0, self.latency),
            volatility=max(0.0, self.volatility),
            incoherence=max(0.0, self.incoherence),
        )


class QuoteEngine:
    """Builds inventory-skewed, risk-widened two-sided quotes from a fair value.

    Parameters
    ----------
    base_half_spread:
        The half-spread RAVEN shows in calm conditions, in probability units.
        e.g. ``0.009`` -> a ~1.8% round-trip spread on a coin-flip outcome.
    min_half_spread / max_half_spread:
        Hard floor and ceiling on the half-spread after all widening.
    skew_gain:
        How hard inventory shifts the quote centre, in probability units at full
        inventory (``|skew| == 1``). Applied as a *shift of both sides together*
        so it changes where RAVEN wants to trade, not how wide it is.
    max_position:
        Soft per-outcome position limit that defines "full" for the skew signal
        and the fractional-Kelly size cap.
    base_size:
        Baseline size shown per side before Kelly/inventory scaling.
    kelly_fraction:
        Fraction of full Kelly used as the *capital-usage ceiling* on size
        (FR3.4). Never touches price.
    hazard_gain / latency_gain / vol_gain / incoherence_gain:
        Additive sensitivity of the half-spread to each risk signal.
    expiry_ms:
        Quote lifetime in calm conditions; shortened as risk rises so stale
        quotes cannot be picked off.
    """

    def __init__(
        self,
        *,
        base_half_spread: float = 0.009,
        min_half_spread: float = 0.004,
        max_half_spread: float = 0.15,
        skew_gain: float = 0.010,
        max_position: float = 1000.0,
        base_size: float = 100.0,
        kelly_fraction: float = 0.25,
        hazard_gain: float = 0.020,
        latency_gain: float = 0.030,
        vol_gain: float = 0.040,
        incoherence_gain: float = 0.050,
        expiry_ms: int = 3000,
    ) -> None:
        if min_half_spread < 0.0 or max_half_spread < min_half_spread:
            raise ValueError("require 0 <= min_half_spread <= max_half_spread")
        if not 0.0 <= kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in [0, 1]")
        if max_position <= 0.0:
            raise ValueError("max_position must be positive")
        self.base_half_spread = base_half_spread
        self.min_half_spread = min_half_spread
        self.max_half_spread = max_half_spread
        self.skew_gain = skew_gain
        self.max_position = max_position
        self.base_size = base_size
        self.kelly_fraction = kelly_fraction
        self.hazard_gain = hazard_gain
        self.latency_gain = latency_gain
        self.vol_gain = vol_gain
        self.incoherence_gain = incoherence_gain
        self.expiry_ms = expiry_ms

    # -- public API ---------------------------------------------------------

    def quote(
        self,
        fair: FairValue,
        inventory: Inventory,
        *,
        risk: Optional[SpreadInputs] = None,
        withdraw: bool = False,
        withdraw_reason: Optional[str] = None,
    ) -> QuoteSet:
        """Produce a :class:`QuoteSet` for one market.

        When ``withdraw`` is set (the Risk Kernel's WITHDRAW state), every
        outcome is returned as a withdrawn quote with zero size but with prices
        still populated for display/audit. Otherwise each outcome is quoted with
        an inventory-skewed centre and a risk-widened half-spread.
        """
        risk = (risk or SpreadInputs()).clamp()
        half_spread = self._half_spread(risk)
        expiry = self._expiry(risk)

        quotes: Dict[str, Quote] = {}
        for outcome, p in fair.probabilities.items():
            quotes[outcome] = self._quote_outcome(
                market=fair.market,
                outcome=outcome,
                fair_prob=p,
                inventory=inventory,
                half_spread=half_spread,
                expiry=expiry,
                withdraw=withdraw,
                withdraw_reason=withdraw_reason,
            )

        return QuoteSet(
            market=fair.market,
            quotes=quotes,
            half_spread=half_spread,
            withdrawn=withdraw,
        )

    # -- internals ----------------------------------------------------------

    def _half_spread(self, risk: SpreadInputs) -> float:
        """Base half-spread widened additively by each risk signal (FR3.3)."""
        widened = (
            self.base_half_spread
            + self.hazard_gain * risk.event_hazard
            + self.latency_gain * risk.latency
            + self.vol_gain * risk.volatility
            + self.incoherence_gain * risk.incoherence
        )
        return max(self.min_half_spread, min(self.max_half_spread, widened))

    def _expiry(self, risk: SpreadInputs) -> int:
        """Shorten quote lifetime as risk rises so stale quotes expire fast."""
        stress = (
            risk.event_hazard + risk.latency + risk.volatility + risk.incoherence
        )
        # Halve the lifetime for every unit of aggregate stress, down to 250ms.
        scaled = self.expiry_ms / (1.0 + stress)
        return int(max(250.0, scaled))

    def _quote_outcome(
        self,
        *,
        market: str,
        outcome: str,
        fair_prob: float,
        inventory: Inventory,
        half_spread: float,
        expiry: int,
        withdraw: bool,
        withdraw_reason: Optional[str],
    ) -> Quote:
        # Inventory skew in [-1, 1]: +1 means we're long and want to sell.
        skew = inventory.skew(market, outcome, self.max_position)
        # Shift the whole quote *against* our position: long -> quote lower.
        centre = fair_prob - self.skew_gain * skew
        centre = max(0.0, min(1.0, centre))

        bid = max(0.0, centre - half_spread)
        ask = min(1.0, centre + half_spread)

        if withdraw:
            return Quote(
                market=market,
                outcome=outcome,
                fair=fair_prob,
                bid=bid,
                ask=ask,
                bid_size=0.0,
                ask_size=0.0,
                expiry_ms=expiry,
                withdrawn=True,
                reason=withdraw_reason or "risk_withdraw",
            )

        bid_size, ask_size = self._sizes(
            market=market, outcome=outcome, fair_prob=fair_prob, skew=skew
        )
        return Quote(
            market=market,
            outcome=outcome,
            fair=fair_prob,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            expiry_ms=expiry,
        )

    def _sizes(
        self, *, market: str, outcome: str, fair_prob: float, skew: float
    ) -> tuple[float, float]:
        """Per-side size: base, capped by fractional Kelly, tilted by inventory.

        Fractional Kelly sets the *ceiling* on how much capital we are willing
        to show (FR3.4) — it never moves the price. We then tilt the two sides
        by current inventory so the side that *reduces* our position shows more
        size than the side that would grow it.
        """
        # Kelly fraction for an even-money-ish outcome priced at fair_prob.
        # b = (1 - p) / p (fair decimal odds minus 1); f* = (b*p - q) / b.
        p = min(max(fair_prob, 1e-6), 1.0 - 1e-6)
        q = 1.0 - p
        b = q / p
        kelly = max(0.0, (b * p - q) / b) if b > 0.0 else 0.0
        cap = self.kelly_fraction * kelly

        size = self.base_size * (0.25 + cap)  # floor so we always show something

        # Tilt: if long (skew > 0) show more on the ask (we want to sell), less
        # on the bid; symmetric when short.
        tilt = max(-0.75, min(0.75, skew))
        bid_size = size * (1.0 - tilt)
        ask_size = size * (1.0 + tilt)
        return round(bid_size, 4), round(ask_size, 4)
