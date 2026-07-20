"""Normalized TxLINE data model for RAVEN's Verified Feed Layer (F1).

TxLINE exposes a single normalized JSON schema across all competitions. RAVEN
maps every inbound frame into a small, explicit set of typed structures so that
downstream layers (Fair-Value, Quote, Risk Kernel) never touch raw JSON.

Two design guarantees live here:

1. Determinism — a frame's ``payload_hash`` is a stable SHA-256 over the raw
   payload with sorted keys. The same real TxLINE bytes always produce the same
   hash, which is what makes the deterministic replay (F8) and the on-chain
   decision receipts (F7) verifiable.

2. Provenance — every frame carries its TxLINE ``sequence`` and (when known) a
   Solana validation reference, so any consumer can prove *which* data point a
   decision was based on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


class FrameKind(str, Enum):
    """The category of an inbound normalized frame."""

    ODDS = "odds"
    SCORE = "score"
    EVENT = "event"
    STATUS = "status"
    UNKNOWN = "unknown"


class MatchEventType(str, Enum):
    """Match events that materially move connected markets.

    These are the shocks RAVEN's Risk Kernel (F4) reacts to. Anything not in
    this set is treated as an informational update, not a shock.
    """

    GOAL = "GOAL"
    RED_CARD = "RED_CARD"
    PENALTY_AWARDED = "PENALTY_AWARDED"
    VAR_REVIEW = "VAR_REVIEW"
    VAR_OVERTURN = "VAR_OVERTURN"
    GAME_FINALISED = "GAME_FINALISED"
    OTHER = "OTHER"

    @classmethod
    def from_raw(cls, value: str) -> "MatchEventType":
        if not value:
            return cls.OTHER
        key = str(value).strip().upper().replace(" ", "_")
        try:
            return cls(key)
        except ValueError:
            return cls.OTHER

    @property
    def is_shock(self) -> bool:
        """True for events that force RAVEN to withdraw / re-price."""
        return self in {
            MatchEventType.GOAL,
            MatchEventType.RED_CARD,
            MatchEventType.PENALTY_AWARDED,
            MatchEventType.VAR_OVERTURN,
        }


def canonical_hash(payload: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over a payload with sorted keys.

    Uses compact separators and ``sort_keys=True`` so semantically identical
    payloads hash identically regardless of key ordering or whitespace.
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Score:
    """Current match score."""

    home: int = 0
    away: int = 0

    def as_tuple(self) -> Tuple[int, int]:
        return (self.home, self.away)


@dataclass(frozen=True)
class OddsSnapshot:
    """Consensus decimal odds for a single market at a point in time.

    ``outcomes`` maps an outcome label (e.g. "home", "draw", "away",
    "over_2_5") to its decimal odds. Kept generic so the same structure serves
    1X2, Total Goals, and Asian Handicap markets.
    """

    market: str
    outcomes: Dict[str, float] = field(default_factory=dict)

    def implied_raw(self) -> Dict[str, float]:
        """Raw implied probabilities (1 / decimal odds), still containing vig."""
        return {
            k: (1.0 / v) for k, v in self.outcomes.items() if v and v > 0.0
        }


@dataclass(frozen=True)
class VerifiedFrame:
    """A single normalized, provenance-tagged TxLINE frame.

    This is the atomic unit that flows through RAVEN. Every downstream layer
    consumes ``VerifiedFrame`` instances, never raw JSON.
    """

    # Provenance
    sequence: int
    timestamp_ms: int
    payload_hash: str
    provider_sequence: Optional[int] = None
    fixture_id: Optional[int] = None
    solana_validation_ref: Optional[str] = None

    # Classification
    kind: FrameKind = FrameKind.UNKNOWN

    # Payloads (only the relevant one(s) are populated per frame)
    score: Optional[Score] = None
    odds: Optional[OddsSnapshot] = None
    event_type: MatchEventType = MatchEventType.OTHER

    # Match clock / status
    match_time: Optional[str] = None
    status_id: Optional[int] = None
    period: Optional[int] = None

    # The untouched raw payload, retained for audit / receipt hashing.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_shock(self) -> bool:
        return self.kind is FrameKind.EVENT and self.event_type.is_shock

    @property
    def is_final(self) -> bool:
        """Match finalization per TxLINE conventions.

        TxLINE marks a completed match via ``game_finalised`` /
        ``statusId=100`` / ``period=100`` regardless of extra time or penalties.
        """
        if self.event_type is MatchEventType.GAME_FINALISED:
            return True
        return self.status_id == 100 or self.period == 100

    @property
    def verified(self) -> bool:
        """True once an on-chain validation reference is attached."""
        return self.solana_validation_ref is not None

    def short_provenance(self) -> str:
        """Compact provenance string for the Control Room badge."""
        if self.verified and self.provider_sequence is not None:
            return f"on-chain verified · TxLINE seq #{self.provider_sequence}"
        if self.provider_sequence is not None:
            return f"TxLINE seq #{self.provider_sequence} · payload bound"
        return f"TxLINE historical payload · replay #{self.sequence}"
