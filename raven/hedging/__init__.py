"""Self-Hedging Engine (F6) — RAVEN's Exposure Neutralizer.

This package treats every open position across *connected* markets as one
portfolio, measures how much RAVEN stands to lose under each discrete match
shock (a home goal, an away goal, a sending-off, or the match settling with no
further goals), and picks a concrete cross-market hedge that shrinks the worst
of those shocks without giving back all of the spread it has earned.
"""

from raven.hedging.engine import (
    HedgeEngine,
    HedgePlan,
    HedgeTrade,
    ShockExposure,
)

__all__ = [
    "HedgeEngine",
    "HedgePlan",
    "HedgeTrade",
    "ShockExposure",
]
