"""Position & inventory tracking for RAVEN.

Inventory is shared state between two layers:

* the **Quote Engine (F3)**, which skews its two-sided quotes against whatever
  it is already holding so the book does not grow lopsided; and
* the **Self-Hedging Engine (F6)**, which reads the whole portfolio to compute
  cross-market event-shock exposure and choose a neutralizing hedge.

Everything here is deterministic and side-effect free apart from
:meth:`Inventory.apply_fill`, which mutates the book when a quote is filled.
Prices are expressed in *probability space* (``0..1``): an outcome contract that
settles at ``1.0`` if the outcome occurs and ``0.0`` otherwise. A ``quantity``
is signed — positive means RAVEN is **long** the outcome (it profits if the
outcome happens), negative means **short**.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, Tuple


# (market, outcome) — lower-cased at the boundary so lookups are robust.
OutcomeKey = Tuple[str, str]


def _key(market: str, outcome: str) -> OutcomeKey:
    return (str(market).strip().lower(), str(outcome).strip().lower())


@dataclass
class Position:
    """A single open position in one market outcome.

    ``avg_price`` is the volume-weighted average probability price of the
    currently open ``quantity``. It is only meaningful while ``quantity`` is
    non-zero; a flat position resets it to ``0.0``.
    """

    market: str
    outcome: str
    quantity: float = 0.0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-12

    def notional(self) -> float:
        """Capital tied up in the open position (``|qty| * avg_price``)."""
        return abs(self.quantity) * self.avg_price


@dataclass
class Inventory:
    """The full book of open positions, keyed by ``(market, outcome)``.

    The class is intentionally small: a getter, a fill mutator, a normalized
    skew signal for the Quote Engine, and a couple of portfolio aggregates for
    the Hedging Engine.
    """

    _positions: Dict[OutcomeKey, Position] = field(default_factory=dict)

    # -- reads --------------------------------------------------------------

    def position(self, market: str, outcome: str) -> Position:
        """Return the (possibly flat) position for an outcome."""
        k = _key(market, outcome)
        pos = self._positions.get(k)
        if pos is None:
            pos = Position(market=k[0], outcome=k[1])
            self._positions[k] = pos
        return pos

    def net_quantity(self, market: str, outcome: str) -> float:
        return self.position(market, outcome).quantity

    def skew(self, market: str, outcome: str, max_position: float) -> float:
        """Normalized inventory signal in ``[-1, 1]`` for quote skewing.

        ``+1`` means RAVEN is maximally long the outcome (and should quote
        *lower* to shed risk); ``-1`` means maximally short. ``max_position``
        is the soft position limit that defines "full".
        """
        if max_position <= 0.0:
            return 0.0
        raw = self.net_quantity(market, outcome) / max_position
        return max(-1.0, min(1.0, raw))

    def total_notional(self) -> float:
        return sum(p.notional() for p in self._positions.values())

    def state_hash(self) -> str:
        """Deterministic SHA-256 digest of the current book.

        Used by the Provenance layer (F7) to anchor the ``inventory_before`` and
        ``inventory_after`` fields of a decision receipt. The digest must be
        stable across identical replays, so positions are serialized in a
        canonical order (sorted by key) and numeric fields are rounded to a
        fixed precision to strip float-representation noise. Flat positions are
        skipped so an untouched outcome never changes the hash.
        """
        parts = []
        for k in sorted(self._positions):
            pos = self._positions[k]
            if pos.is_flat:
                continue
            parts.append(f"{k[0]}|{k[1]}|{pos.quantity:.9f}|{pos.avg_price:.9f}")
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return digest

    def __iter__(self) -> Iterator[Position]:
        return iter(self._positions.values())


    # -- writes -------------------------------------------------------------

    def apply_fill(
        self, market: str, outcome: str, quantity: float, price: float
    ) -> None:
        """Update the book for a fill of ``quantity`` at ``price``.

        ``quantity`` is signed (positive = we bought the outcome, negative = we
        sold/short it). The average price is maintained VWAP-style while adding
        to a position, held constant while reducing it, and re-based to the
        fill price if the position flips through zero.
        """
        if quantity == 0.0:
            return
        pos = self.position(market, outcome)
        q0 = pos.quantity

        # Adding to (or opening) a position in the same direction.
        if q0 == 0.0 or (q0 > 0.0) == (quantity > 0.0):
            new_q = q0 + quantity
            if new_q != 0.0:
                pos.avg_price = (q0 * pos.avg_price + quantity * price) / new_q
            pos.quantity = new_q
            return

        # Opposite direction: reducing, closing, or flipping.
        new_q = q0 + quantity
        if (new_q > 0.0) == (q0 > 0.0) or new_q == 0.0:
            # Still on the same side (a reduction) or exactly flat: the average
            # entry price of the remaining position is unchanged.
            pos.quantity = new_q
            if pos.is_flat:
                pos.avg_price = 0.0
        else:
            # Flipped through zero: the residual takes the new fill's price.
            pos.quantity = new_q
            pos.avg_price = price
