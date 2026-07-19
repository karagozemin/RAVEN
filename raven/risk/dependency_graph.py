"""Market Dependency Graph (F5).

Every football market RAVEN quotes is connected: a single home goal re-prices
Match Winner, Asian Handicap, and Total Goals all at once.  The naive per-market
view misses this — if 1X2 updates but Handicap stays stale for >2 s, that is an
exploitable window that informed traders will hit.

The graph captures those relationships explicitly:

* Each market is a **node** with an expected directional response to each shock.
* Relationships between markets are **edges** — e.g. "a home goal that lifts Home
  Win should also lift Over in Total Goals."
* After every verified match event, :meth:`DependencyGraph.check` scans all
  dependent nodes and flags any that have not moved in the expected direction
  within ``staleness_threshold_ms`` milliseconds.

A stale linked market is the trigger for the Risk Kernel to escalate from CAUTION
to WITHDRAW — not just the event itself — because the stale quote is where margin
leaks.

The graph also powers the cross-market coherence term in the risk-score formula:

    risk = 0.30·consensusDev + 0.25·eventLatency
         + 0.20·crossMarketIncoherence + 0.15·exposure + 0.10·feedConfidence

:attr:`CoherenceResult.score` maps directly to that ``crossMarketIncoherence``
component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from raven.feed.model import MatchEventType, OddsSnapshot, VerifiedFrame


# ---------------------------------------------------------------------------
# Shock → expected direction per outcome
# ---------------------------------------------------------------------------

class ExpectedDirection(str, Enum):
    """Whether an outcome's probability should rise, fall, or be unaffected."""
    UP   = "up"
    DOWN = "down"
    FLAT = "flat"


# Maps (market_family, outcome_key) → {shock → ExpectedDirection}
# Market families: "match_winner", "total_goals", "asian_handicap"
_EXPECTED: Dict[Tuple[str, str], Dict[MatchEventType, ExpectedDirection]] = {
    # Match Winner ─────────────────────────────────────────────────────────
    ("match_winner", "home"): {
        MatchEventType.GOAL:            ExpectedDirection.UP,
        MatchEventType.RED_CARD:        ExpectedDirection.DOWN,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.UP,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    ("match_winner", "draw"): {
        MatchEventType.GOAL:            ExpectedDirection.DOWN,
        MatchEventType.RED_CARD:        ExpectedDirection.UP,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.DOWN,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    ("match_winner", "away"): {
        MatchEventType.GOAL:            ExpectedDirection.DOWN,
        MatchEventType.RED_CARD:        ExpectedDirection.UP,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.DOWN,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    # Total Goals ───────────────────────────────────────────────────────────
    ("total_goals", "over"): {
        MatchEventType.GOAL:            ExpectedDirection.UP,
        MatchEventType.RED_CARD:        ExpectedDirection.DOWN,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.UP,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    ("total_goals", "under"): {
        MatchEventType.GOAL:            ExpectedDirection.DOWN,
        MatchEventType.RED_CARD:        ExpectedDirection.UP,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.DOWN,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    # Asian Handicap ────────────────────────────────────────────────────────
    ("asian_handicap", "home"): {
        MatchEventType.GOAL:            ExpectedDirection.UP,
        MatchEventType.RED_CARD:        ExpectedDirection.DOWN,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.UP,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
    ("asian_handicap", "away"): {
        MatchEventType.GOAL:            ExpectedDirection.DOWN,
        MatchEventType.RED_CARD:        ExpectedDirection.UP,
        MatchEventType.PENALTY_AWARDED: ExpectedDirection.DOWN,
        MatchEventType.VAR_OVERTURN:    ExpectedDirection.FLAT,
    },
}

# Which markets are "connected" to which — used to enumerate edges.
CONNECTED_MARKETS: List[str] = ["match_winner", "total_goals", "asian_handicap"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketState:
    """Latest odds snapshot for one market."""
    market: str
    odds: Dict[str, float] = field(default_factory=dict)
    last_update_ms: int = 0


@dataclass(frozen=True)
class StalenessFlag:
    """One stale-linked-market detection."""
    market: str
    outcome: str
    expected: ExpectedDirection
    elapsed_ms: float
    note: str


@dataclass(frozen=True)
class CoherenceResult:
    """Outcome of a post-event coherence check across all connected markets.

    ``score`` is in [0, 1]: 0 = perfectly coherent, 1 = maximally incoherent.
    It feeds directly into the crossMarketIncoherence risk term.
    ``stale_markets`` lists every market that was still stale at check time.
    """
    score: float
    stale_markets: List[StalenessFlag]
    event: MatchEventType
    checked_at_ms: int

    @property
    def has_stale(self) -> bool:
        return bool(self.stale_markets)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

class DependencyGraph:
    """Tracks connected market states and detects post-event stale quotes.

    Usage
    -----
    Feed every normalized frame into :meth:`update`.
    After a shock event, call :meth:`check` with the event type and the
    timestamp to get a :class:`CoherenceResult` that quantifies how coherent
    the linked markets are.
    """

    def __init__(self, staleness_threshold_ms: float = 2_000.0) -> None:
        """
        Parameters
        ----------
        staleness_threshold_ms:
            How long (wall-clock ms) a linked market is allowed to go without
            updating after a verified shock before it is considered stale.
            PRD target NFR1 says withdrawal < 250 ms; stale detection is a bit
            more lenient (default 2 s) because market-data pipes have latency.
        """
        self._threshold = staleness_threshold_ms
        self._states: Dict[str, MarketState] = {
            m: MarketState(market=m) for m in CONNECTED_MARKETS
        }
        self._last_event_ms: int = 0
        self._last_event_type: Optional[MatchEventType] = None

    # ------------------------------------------------------------------
    # Feed ingestion
    # ------------------------------------------------------------------

    def update(self, frame: VerifiedFrame) -> None:
        """Ingest a normalized frame and update the relevant market state."""
        if frame.odds is None:
            return
        market = _canonical_market(frame.odds.market)
        if market not in self._states:
            return
        state = self._states[market]
        # Store implied probabilities (vig-inclusive) for direction comparison.
        state.odds = dict(frame.odds.implied_raw())
        state.last_update_ms = frame.timestamp_ms

    def record_event(self, frame: VerifiedFrame) -> None:
        """Record the timestamp of a verified shock event."""
        if frame.is_shock:
            self._last_event_ms = frame.timestamp_ms
            self._last_event_type = frame.event_type

    # ------------------------------------------------------------------
    # Coherence check
    # ------------------------------------------------------------------

    def check(
        self,
        event: MatchEventType,
        now_ms: int,
        prev_odds: Optional[Dict[str, MarketState]] = None,
    ) -> CoherenceResult:
        """Check all connected markets for post-event coherence.

        Parameters
        ----------
        event:
            The shock that just occurred.
        now_ms:
            Current wall-clock millisecond timestamp.
        prev_odds:
            Optional snapshot of market odds *before* the event, used to verify
            directional movement. If absent, only staleness (time since last
            update) is checked.
        """
        stale: List[StalenessFlag] = []
        total_checks = 0
        stale_count = 0

        for market, state in self._states.items():
            outcomes_to_check = _outcomes_for_market(market)
            for outcome in outcomes_to_check:
                key = (market, outcome)
                expected_dir = _EXPECTED.get(key, {}).get(event, ExpectedDirection.FLAT)
                if expected_dir is ExpectedDirection.FLAT:
                    continue

                total_checks += 1
                elapsed = float(now_ms - state.last_update_ms)

                # Primary staleness: market hasn't ticked since the event.
                if elapsed > self._threshold:
                    stale.append(StalenessFlag(
                        market=market,
                        outcome=outcome,
                        expected=expected_dir,
                        elapsed_ms=elapsed,
                        note=(
                            f"{market}.{outcome} stale {elapsed:.0f} ms "
                            f"after {event.value}"
                        ),
                    ))
                    stale_count += 1
                    continue

                # Secondary: market ticked but moved wrong direction.
                if prev_odds is not None:
                    prev_state = prev_odds.get(market)
                    if prev_state is not None:
                        moved_wrong = _wrong_direction(
                            outcome, expected_dir,
                            prev_state.odds, state.odds,
                        )
                        if moved_wrong:
                            stale.append(StalenessFlag(
                                market=market,
                                outcome=outcome,
                                expected=expected_dir,
                                elapsed_ms=elapsed,
                                note=(
                                    f"{market}.{outcome} moved wrong direction "
                                    f"after {event.value}"
                                ),
                            ))
                            stale_count += 1

        score = stale_count / max(total_checks, 1)
        return CoherenceResult(
            score=round(score, 4),
            stale_markets=stale,
            event=event,
            checked_at_ms=now_ms,
        )

    def snapshot(self) -> Dict[str, MarketState]:
        """Return a shallow copy of current market states (for prev_odds)."""
        return {
            m: MarketState(
                market=s.market,
                odds=dict(s.odds),
                last_update_ms=s.last_update_ms,
            )
            for m, s in self._states.items()
        }

    def summary(self) -> str:
        """One-line summary for the Control Room terminal."""
        parts = []
        for m, s in self._states.items():
            age_s = "?"
            if s.last_update_ms:
                pass  # age would need wall time — show market name only
            parts.append(m[:3].upper())
        return "dep-graph: " + " | ".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_market(raw: str) -> str:
    """Normalize arbitrary market names to our three canonical keys."""
    r = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if "winner" in r or "1x2" in r or r in {"match_result", "1_x_2"}:
        return "match_winner"
    if "total" in r or "goal" in r or "over" in r or "under" in r:
        return "total_goals"
    if "handicap" in r or "ah" in r:
        return "asian_handicap"
    return r


def _outcomes_for_market(market: str) -> List[str]:
    keys = [k[1] for k in _EXPECTED if k[0] == market]
    return keys


def _wrong_direction(
    outcome: str,
    expected: ExpectedDirection,
    prev: Dict[str, float],
    curr: Dict[str, float],
) -> bool:
    """Return True if the outcome moved in the wrong direction."""
    p_prev = prev.get(outcome)
    p_curr = curr.get(outcome)
    if p_prev is None or p_curr is None:
        return False
    delta = p_curr - p_prev
    if abs(delta) < 1e-4:
        return False  # Negligible movement — not conclusive.
    if expected is ExpectedDirection.UP and delta < 0:
        return True
    if expected is ExpectedDirection.DOWN and delta > 0:
        return True
    return False
