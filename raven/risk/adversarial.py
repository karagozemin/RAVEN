"""Adversarial Flow Detector (F9).

In a live in-play market the biggest threat to a market maker is not the match
itself — it is *informed order flow*.  Before a goal is published on the feed,
insiders (or low-latency scrapers) know.  They hit the stale side of RAVEN's
book repeatedly and at scale.  By the time the feed event arrives the damage is
done.

The Adversarial Flow Detector watches *how* fills arrive, not just *what* they
say, and builds a running toxicity score per market.  When that score crosses a
threshold RAVEN starts defending: smaller size, wider spread, shorter expiry —
and ultimately full quote withdrawal — *before* the event frame lands.

Detection signals
-----------------
1. **Side concentration** — the rolling fraction of fills landing on one side
   (buy or sell).  Sharp players all take the same side before a move.
2. **Fill velocity** — fills per second above the baseline.  Toxic flow comes
   in bursts; normal retail flow is diffuse.
3. **Repeat-aggressor ratio** — how often fills come from the same synthetic
   aggressor bucket (proxied by the sequence of fill sizes).  Informed flow is
   repetitive in size.

Toxicity → action mapping
--------------------------
0.00 – 0.30  CLEAN    — normal quoting
0.30 – 0.60  SUSPECT  — reduce max_size by 50 %, widen by +0.5 %
0.60 – 0.80  TOXIC    — reduce by 75 %, widen by +1.5 %, shorten expiry
0.80 – 1.00  HOSTILE  — full withdrawal (same effect as WITHDRAW state)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class ToxicityLevel(str, Enum):
    CLEAN   = "CLEAN"
    SUSPECT = "SUSPECT"
    TOXIC   = "TOXIC"
    HOSTILE = "HOSTILE"


@dataclass(frozen=True)
class FlowSignal:
    """Snapshot of adversarial-flow detection for one market.

    Attributes
    ----------
    market:
        The canonical market key ("match_winner", etc.).
    toxicity_score:
        Composite score in [0, 1].
    level:
        Human-readable tier derived from the score.
    size_cap_fraction:
        Fraction of normal max_size RAVEN should use (1.0 = unchanged).
    spread_addon:
        Additional spread (absolute probability units) to add on top of the
        normal quote-engine spread.
    shorten_expiry:
        If True, halve the quote expiry time.
    withdraw:
        If True, stop quoting this market entirely (HOSTILE level).
    side_concentration:
        Rolling one-sided fill fraction (0.5 = balanced, 1.0 = all one side).
    fill_velocity:
        Fills per second in the recent window.
    """

    market: str
    toxicity_score: float
    level: ToxicityLevel
    size_cap_fraction: float
    spread_addon: float
    shorten_expiry: bool
    withdraw: bool
    side_concentration: float
    fill_velocity: float


# ---------------------------------------------------------------------------
# Fill record (tiny, deterministic)
# ---------------------------------------------------------------------------

@dataclass
class FillRecord:
    """A single observed fill on RAVEN's quotes.

    Parameters
    ----------
    market:   Canonical market key.
    outcome:  Outcome hit (e.g. "home", "over").
    side:     "buy" (aggressor is buying, i.e. taking RAVEN's ask) or
              "sell" (aggressor taking RAVEN's bid).
    size:     Notional quantity.
    ts_ms:    Wall-clock millisecond timestamp of the fill.
    """

    market: str
    outcome: str
    side: str  # "buy" | "sell"
    size: float
    ts_ms: int


# ---------------------------------------------------------------------------
# Per-market ring buffer
# ---------------------------------------------------------------------------

_WINDOW_MS   = 30_000   # 30-second rolling window
_MAX_RECORDS = 200      # cap on stored fills per market


class _MarketFlowWindow:
    """Rolling fill history for one market."""

    def __init__(self) -> None:
        self._fills: Deque[FillRecord] = deque(maxlen=_MAX_RECORDS)

    def record(self, fill: FillRecord) -> None:
        self._fills.append(fill)

    def _recent(self, now_ms: int) -> list[FillRecord]:
        cutoff = now_ms - _WINDOW_MS
        return [f for f in self._fills if f.ts_ms >= cutoff]

    # -- signal derivation --------------------------------------------------

    def side_concentration(self, now_ms: int) -> float:
        """Fraction in [0.5, 1.0]: how one-sided are recent fills?"""
        recent = self._recent(now_ms)
        if len(recent) < 3:
            return 0.5
        buys = sum(1 for f in recent if f.side == "buy")
        total = len(recent)
        dominant = max(buys, total - buys)
        return dominant / total

    def fill_velocity(self, now_ms: int) -> float:
        """Fills per second in the rolling window."""
        recent = self._recent(now_ms)
        if len(recent) < 2:
            return 0.0
        window_s = _WINDOW_MS / 1_000.0
        return len(recent) / window_s

    def repeat_aggressor_ratio(self, now_ms: int) -> float:
        """Heuristic: fraction of fills whose size appeared >1× in the window.

        Real-world systems use account IDs; we proxy with size buckets.
        """
        recent = self._recent(now_ms)
        if len(recent) < 4:
            return 0.0
        # Bucket sizes to 2 s.f.
        buckets: Dict[float, int] = {}
        for f in recent:
            key = round(f.size, 1)
            buckets[key] = buckets.get(key, 0) + 1
        repeated = sum(1 for f in recent if buckets[round(f.size, 1)] > 1)
        return repeated / len(recent)

    def composite_score(self, now_ms: int, baseline_vps: float = 0.5) -> float:
        """Weighted composite toxicity score in [0, 1]."""
        sc  = self.side_concentration(now_ms)     # 0.5 → 1.0
        vel = self.fill_velocity(now_ms)           # fills/s
        rar = self.repeat_aggressor_ratio(now_ms)  # 0 → 1

        # Normalise side-concentration: 0.5 = clean, 1.0 = max suspect.
        sc_norm  = (sc - 0.5) * 2.0                # [0, 1]
        vel_norm = min(1.0, vel / max(baseline_vps * 5, 1.0))  # burst factor

        score = 0.45 * sc_norm + 0.35 * vel_norm + 0.20 * rar
        return min(1.0, max(0.0, round(score, 4)))


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

_SUSPECT_THRESHOLD = 0.30
_TOXIC_THRESHOLD   = 0.60
_HOSTILE_THRESHOLD = 0.80


class AdversarialFlowDetector:
    """Tracks fill behaviour across all markets and emits FlowSignals.

    Usage
    -----
    1. Call :meth:`record_fill` every time a fill hits RAVEN's book.
    2. Call :meth:`assess` every tick to get the current :class:`FlowSignal`
       per market.  The agent feeds these into the quote engine to adjust
       spread and size.
    """

    def __init__(self, baseline_fills_per_s: float = 0.5) -> None:
        """
        Parameters
        ----------
        baseline_fills_per_s:
            Expected quiescent fill rate (fills/second) used to normalise the
            velocity signal.  0.5 fill/s is conservative for a small devnet MM.
        """
        self._baseline_vps = max(0.01, baseline_fills_per_s)
        self._windows: Dict[str, _MarketFlowWindow] = {}

    # -- ingestion ----------------------------------------------------------

    def record_fill(self, fill: FillRecord) -> None:
        """Register a fill event."""
        win = self._windows.setdefault(fill.market, _MarketFlowWindow())
        win.record(fill)

    # -- assessment ---------------------------------------------------------

    def assess(self, market: str, now_ms: Optional[int] = None) -> FlowSignal:
        """Return the current flow signal for *market*.

        If no fills have been seen yet for this market the signal is CLEAN with
        defaults.
        """
        if now_ms is None:
            now_ms = _now_ms()

        win = self._windows.get(market)
        if win is None:
            return _clean_signal(market)

        score = win.composite_score(now_ms, self._baseline_vps)
        level, size_cap, spread_addon, shorten, withdraw = _level(score)

        return FlowSignal(
            market=market,
            toxicity_score=score,
            level=level,
            size_cap_fraction=size_cap,
            spread_addon=spread_addon,
            shorten_expiry=shorten,
            withdraw=withdraw,
            side_concentration=win.side_concentration(now_ms),
            fill_velocity=win.fill_velocity(now_ms),
        )

    def assess_all(self, now_ms: Optional[int] = None) -> Dict[str, FlowSignal]:
        """Return signals for every market that has seen fills."""
        if now_ms is None:
            now_ms = _now_ms()
        return {m: self.assess(m, now_ms) for m in self._windows}

    def worst_level(self, now_ms: Optional[int] = None) -> ToxicityLevel:
        """The most severe toxicity level across all tracked markets."""
        if now_ms is None:
            now_ms = _now_ms()
        signals = self.assess_all(now_ms)
        if not signals:
            return ToxicityLevel.CLEAN
        order = [
            ToxicityLevel.CLEAN,
            ToxicityLevel.SUSPECT,
            ToxicityLevel.TOXIC,
            ToxicityLevel.HOSTILE,
        ]
        worst = ToxicityLevel.CLEAN
        for sig in signals.values():
            if order.index(sig.level) > order.index(worst):
                worst = sig.level
        return worst

    def summary_line(self, now_ms: Optional[int] = None) -> str:
        """Compact one-liner for the Control Room terminal."""
        if now_ms is None:
            now_ms = _now_ms()
        parts = []
        for m, sig in self.assess_all(now_ms).items():
            parts.append(
                f"{m[:3].upper()} [{sig.level.value}|{sig.toxicity_score:.2f}]"
            )
        return "adversarial: " + " | ".join(parts) if parts else "adversarial: clean"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _level(
    score: float,
) -> Tuple[ToxicityLevel, float, float, bool, bool]:
    """Map score → (level, size_cap, spread_addon, shorten_expiry, withdraw)."""
    if score >= _HOSTILE_THRESHOLD:
        return ToxicityLevel.HOSTILE, 0.0, 0.0, True, True
    if score >= _TOXIC_THRESHOLD:
        return ToxicityLevel.TOXIC, 0.25, 0.015, True, False
    if score >= _SUSPECT_THRESHOLD:
        return ToxicityLevel.SUSPECT, 0.50, 0.005, False, False
    return ToxicityLevel.CLEAN, 1.0, 0.0, False, False


def _clean_signal(market: str) -> FlowSignal:
    return FlowSignal(
        market=market,
        toxicity_score=0.0,
        level=ToxicityLevel.CLEAN,
        size_cap_fraction=1.0,
        spread_addon=0.0,
        shorten_expiry=False,
        withdraw=False,
        side_concentration=0.5,
        fill_velocity=0.0,
    )


def _now_ms() -> int:
    return int(time.time() * 1_000)
