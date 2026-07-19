"""Raw TxLINE JSON -> VerifiedFrame normalization (F1).

TxLINE advertises a single normalized JSON schema, but real-world feeds still
vary in field naming (camelCase vs snake_case, nested vs flat). This module is
the *one* place that tolerates that variance. Everything downstream consumes
the clean ``VerifiedFrame`` produced here.

The normalizer is intentionally defensive: unknown / partial frames are still
emitted (as ``FrameKind.UNKNOWN``) with valid provenance so nothing is silently
dropped and the deterministic replay stays byte-faithful.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from .model import (
    FrameKind,
    MatchEventType,
    OddsSnapshot,
    Score,
    VerifiedFrame,
    canonical_hash,
)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value among ``keys``."""
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_score(payload: Mapping[str, Any]) -> Optional[Score]:
    score = _first(payload, "score", "scores", "currentScore")
    if isinstance(score, Mapping):
        home = _to_int(_first(score, "home", "h", "homeScore"))
        away = _to_int(_first(score, "away", "a", "awayScore"))
        if home is not None or away is not None:
            return Score(home=home or 0, away=away or 0)
    # Flat form: home_score / away_score at the top level.
    home = _to_int(_first(payload, "home_score", "homeScore"))
    away = _to_int(_first(payload, "away_score", "awayScore"))
    if home is not None or away is not None:
        return Score(home=home or 0, away=away or 0)
    return None


def _extract_odds(payload: Mapping[str, Any]) -> Optional[OddsSnapshot]:
    odds = _first(payload, "odds", "prices", "consensus")
    if not isinstance(odds, Mapping):
        return None
    market = str(
        _first(payload, "market", "marketType", "market_id") or "MATCH_WINNER"
    )
    outcomes: dict[str, float] = {}
    for label, raw_price in odds.items():
        price = _to_float(raw_price)
        if price is not None and price > 1.0:
            outcomes[str(label).strip().lower()] = price
    if not outcomes:
        return None
    return OddsSnapshot(market=market, outcomes=outcomes)


def _classify(
    payload: Mapping[str, Any],
    score: Optional[Score],
    odds: Optional[OddsSnapshot],
    event_type: MatchEventType,
) -> FrameKind:
    raw_kind = _first(payload, "type", "kind", "messageType", "channel")
    if raw_kind:
        rk = str(raw_kind).strip().lower()
        if "odd" in rk or "price" in rk:
            return FrameKind.ODDS
        if "score" in rk:
            return FrameKind.SCORE
        if "event" in rk:
            return FrameKind.EVENT
        if "status" in rk or "fixture" in rk:
            return FrameKind.STATUS
    # Infer from payload contents when no explicit type is present.
    if event_type is not MatchEventType.OTHER:
        return FrameKind.EVENT
    if odds is not None:
        return FrameKind.ODDS
    if score is not None:
        return FrameKind.SCORE
    return FrameKind.UNKNOWN


def normalize(
    payload: Mapping[str, Any],
    *,
    fallback_sequence: int,
    solana_validation_ref: Optional[str] = None,
) -> VerifiedFrame:
    """Map a single raw TxLINE payload into a ``VerifiedFrame``.

    ``fallback_sequence`` is used only when the payload carries no native
    sequence, preserving a strictly increasing order for deterministic replay.
    """
    sequence = _to_int(
        _first(payload, "sequence", "seq", "sequenceNumber")
    )
    if sequence is None:
        sequence = fallback_sequence

    timestamp_ms = _to_int(
        _first(payload, "timestamp", "ts", "timestampMs", "time")
    )
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    # TxLINE may send seconds; upscale to ms if the value looks like seconds.
    if timestamp_ms < 1_000_000_000_000:
        timestamp_ms *= 1000

    fixture_id = _to_int(_first(payload, "fixture_id", "fixtureId", "fixture"))
    event_type = MatchEventType.from_raw(
        str(_first(payload, "event_type", "eventType", "event") or "")
    )
    score = _extract_score(payload)
    odds = _extract_odds(payload)
    kind = _classify(payload, score, odds, event_type)

    return VerifiedFrame(
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload_hash=canonical_hash(payload),
        fixture_id=fixture_id,
        solana_validation_ref=solana_validation_ref,
        kind=kind,
        score=score,
        odds=odds,
        event_type=event_type,
        match_time=(
            str(_first(payload, "match_time", "matchTime", "clock") or "")
            or None
        ),
        status_id=_to_int(_first(payload, "status_id", "statusId", "status")),
        period=_to_int(_first(payload, "period", "phase")),
        raw=dict(payload),
    )
