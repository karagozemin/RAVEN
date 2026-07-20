"""Deterministic matching model for the hackathon execution environment.

TxLINE supplies market data, not a betting exchange order-entry API. This
adapter therefore models fills against RAVEN's published quotes. A quote fills
when the next real consensus update crosses it; low-rate passive flow is derived
from the immutable TxLINE payload hash so replay results remain deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from raven.feed.model import FrameKind, VerifiedFrame
from raven.pricing.vig import remove_vig
from raven.quoting.engine import QuoteSet
from raven.quoting.inventory import Inventory


@dataclass(frozen=True)
class SimulatedFill:
    market: str
    outcome: str
    side: str
    size: float
    price: float
    timestamp_ms: int
    reason: str

    def inventory_quantity(self) -> float:
        # Customer buys at our ask -> RAVEN sells. Customer sells at our bid -> RAVEN buys.
        return -self.size if self.side == "customer_buy" else self.size


class SimulatedExecution:
    """Match real consensus movements against the last published quote set."""

    def __init__(self, passive_fill_fraction: float = 0.18) -> None:
        self.passive_fill_fraction = passive_fill_fraction
        self._quotes: Dict[str, QuoteSet] = {}

    def publish(self, quotes: Dict[str, QuoteSet]) -> None:
        self._quotes = dict(quotes)

    def cancel_all(self) -> int:
        count = sum(len(quote_set.quotes) for quote_set in self._quotes.values())
        self._quotes = {}
        return count

    def process(self, frame: VerifiedFrame, inventory: Inventory) -> List[SimulatedFill]:
        if frame.is_shock or frame.kind is not FrameKind.ODDS or frame.odds is None:
            return []
        quote_set = self._quotes.get(frame.odds.market)
        if quote_set is None or quote_set.withdrawn:
            return []

        consensus = remove_vig(frame.odds.outcomes)
        fills: List[SimulatedFill] = []
        for outcome, quote in quote_set.quotes.items():
            fair = consensus.get(outcome)
            if fair is None or quote.withdrawn:
                continue
            if fair >= quote.ask:
                fills.append(
                    SimulatedFill(
                        market=quote.market,
                        outcome=outcome,
                        side="customer_buy",
                        size=round(quote.ask_size * 0.55, 4),
                        price=quote.ask,
                        timestamp_ms=frame.timestamp_ms,
                        reason="consensus crossed ask",
                    )
                )
            elif fair <= quote.bid:
                fills.append(
                    SimulatedFill(
                        market=quote.market,
                        outcome=outcome,
                        side="customer_sell",
                        size=round(quote.bid_size * 0.55, 4),
                        price=quote.bid,
                        timestamp_ms=frame.timestamp_ms,
                        reason="consensus crossed bid",
                    )
                )

        # Resting orders also receive small passive flow. Selection and side are
        # derived from the raw TxLINE hash, never from wall clock or randomness.
        if not fills and quote_set.quotes:
            selector = int(frame.payload_hash[:12], 16)
            if selector % 7 == 0:
                outcomes = sorted(quote_set.quotes)
                outcome = outcomes[selector % len(outcomes)]
                quote = quote_set.quotes[outcome]
                customer_buys = bool((selector // 7) % 2)
                side = "customer_buy" if customer_buys else "customer_sell"
                price = quote.ask if customer_buys else quote.bid
                displayed = quote.ask_size if customer_buys else quote.bid_size
                fills.append(
                    SimulatedFill(
                        market=quote.market,
                        outcome=outcome,
                        side=side,
                        size=round(displayed * self.passive_fill_fraction, 4),
                        price=price,
                        timestamp_ms=frame.timestamp_ms,
                        reason="deterministic passive match",
                    )
                )

        for fill in fills:
            inventory.apply_fill(
                fill.market,
                fill.outcome,
                fill.inventory_quantity(),
                fill.price,
            )
        return fills
