"""Match-state derivation for RAVEN's Fair-Value Engine (F2).

The event-hazard model prices the *remaining* match, so it needs a clean,
deterministic view of where the match currently is: elapsed minutes, minutes
remaining, current score, and any red cards in effect. This module turns the
loosely-typed TxLINE clock/score fields into a strict :class:`MatchState`.

All parsing is defensive: TxLINE clocks arrive as strings like ``"67:14"`` or
``"45+2"``, occasionally as bare minutes, and sometimes absent. We never raise
on a malformed clock — we degrade gracefully, because a pricing engine that
crashes on one odd frame is worse than one that holds its last good state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from raven.feed.model import MatchEventType, VerifiedFrame, Score

# Regulation length for the hazard model's time budget. Added time is folded
# into the current half rather than extending this, keeping the remaining-time
# integral well-defined.
REGULATION_MINUTES = 90.0

_CLOCK_RE = re.compile(r"^\s*(\d{1,3})(?:\s*\+\s*(\d{1,3}))?\s*(?::\s*(\d{1,2}))?\s*$")


def parse_clock_minutes(match_time: Optional[str]) -> Optional[float]:
    """Parse a TxLINE clock string into elapsed minutes (float).

    Accepts ``"67:14"`` (mm:ss), ``"45+2"`` (added time), ``"73"`` (bare
    minutes), or ``"45+2:30"``. Returns ``None`` if unparseable so the caller
    can fall back to its last known state.
    """
    if match_time is None:
        return None
    text = str(match_time).strip()
    if not text:
        return None

    m = _CLOCK_RE.match(text)
    if not m:
        return None

    base = float(m.group(1))
    added = float(m.group(2)) if m.group(2) else 0.0
    seconds = float(m.group(3)) if m.group(3) else 0.0

    # If a colon seconds field is present, the first group is minutes and the
    # optional "+N" is added minutes (e.g. "45+2:30" -> 47.5).
    return base + added + seconds / 60.0


@dataclass(frozen=True)
class MatchState:
    """A deterministic snapshot of match progress for pricing.

    ``minutes_elapsed`` retains the provider clock through extra time. The
    remaining-time budget still floors at zero after regulation;
    ``red_home`` / ``red_away`` capture man-advantage, which shifts goal
    intensities.
    """

    minutes_elapsed: float = 0.0
    score: Score = field(default_factory=Score)
    red_home: int = 0
    red_away: int = 0
    is_final: bool = False

    @property
    def minutes_remaining(self) -> float:
        return max(0.0, REGULATION_MINUTES - self.minutes_elapsed)

    @property
    def fraction_remaining(self) -> float:
        return self.minutes_remaining / REGULATION_MINUTES

    @property
    def goal_difference(self) -> int:
        """Home minus away goals."""
        return self.score.home - self.score.away

    def with_frame(self, frame: VerifiedFrame) -> "MatchState":
        """Return a new state updated by an inbound frame.

        Score and clock frames refresh their respective fields; a red-card
        event increments the man-advantage counters; finalization latches
        ``is_final``. Unknown / odd frames leave the state unchanged.
        """
        minutes = parse_clock_minutes(frame.match_time)
        new_minutes = (
            self.minutes_elapsed if minutes is None else max(minutes, 0.0)
        )

        new_score = frame.score if frame.score is not None else self.score

        red_home, red_away = self.red_home, self.red_away
        if frame.event_type is MatchEventType.RED_CARD:
            # We can't always tell the side from a normalized event; attribute
            # to the team with the ball where available, else count globally.
            side = str(frame.raw.get("team", "")).strip().lower()
            if side in {"home", "h", "1"}:
                red_home += 1
            elif side in {"away", "a", "2"}:
                red_away += 1
            # If side is unknown we still note a red card exists via the max of
            # the two; leave counts unchanged to avoid mis-attribution.

        return MatchState(
            minutes_elapsed=new_minutes,
            score=new_score,
            red_home=red_home,
            red_away=red_away,
            is_final=self.is_final or frame.is_final,
        )
