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
    score = _first(payload, "Score", "score", "scores", "currentScore")
    if isinstance(score, Mapping):
        home = _to_int(_first(score, "home", "h", "homeScore"))
        away = _to_int(_first(score, "away", "a", "awayScore"))
        if home is not None or away is not None:
            return Score(home=home or 0, away=away or 0)
        # Native TxLINE soccer schema.
        participant1 = score.get("Participant1")
        participant2 = score.get("Participant2")
        if isinstance(participant1, Mapping) or isinstance(participant2, Mapping):
            def participant_goals(participant: Mapping[str, Any]) -> Optional[int]:
                total = participant.get("Total") or participant.get("HT") or participant.get("H1")
                if isinstance(total, Mapping):
                    return _to_int(total.get("Goals"))
                return None

            goals1 = participant_goals(participant1) if isinstance(participant1, Mapping) else 0
            goals2 = participant_goals(participant2) if isinstance(participant2, Mapping) else 0
            if goals1 is not None or goals2 is not None:
                participant1_home = bool(payload.get("Participant1IsHome", True))
                return Score(
                    home=(goals1 if participant1_home else goals2) or 0,
                    away=(goals2 if participant1_home else goals1) or 0,
                )
    # Flat form: home_score / away_score at the top level.
    home = _to_int(_first(payload, "home_score", "homeScore"))
    away = _to_int(_first(payload, "away_score", "awayScore"))
    if home is not None or away is not None:
        return Score(home=home or 0, away=away or 0)
    return None


def _extract_odds(payload: Mapping[str, Any]) -> Optional[OddsSnapshot]:
    odds = _first(payload, "odds", "prices", "consensus")
    if not isinstance(odds, Mapping):
        price_names = payload.get("PriceNames")
        raw_prices = payload.get("Prices")
        super_type = str(payload.get("SuperOddsType") or "")
        if isinstance(price_names, list) and isinstance(raw_prices, list) and super_type:
            aliases = {
                "part1": "home",
                "part2": "away",
                "draw": "draw",
                "over": "over",
                "under": "under",
            }
            converted: dict[str, float] = {}
            for name, raw_price in zip(price_names, raw_prices):
                price = _to_float(raw_price)
                if price is None:
                    continue
                # TxLINE encodes decimal odds in thousandths (1787 -> 1.787).
                if price >= 1000.0:
                    price /= 1000.0
                if price > 1.0:
                    converted[aliases.get(str(name).lower(), str(name).lower())] = price
            market_parameters = str(payload.get("MarketParameters") or "")
            line = market_parameters.split("=", 1)[1] if market_parameters.startswith("line=") else ""
            market_names = {
                "1X2_PARTICIPANT_RESULT": "match_winner",
                "ASIANHANDICAP_PARTICIPANT_GOALS": f"asian_handicap@{line}",
                "OVERUNDER_PARTICIPANT_GOALS": f"total_goals@{line}",
            }
            market = market_names.get(super_type, super_type.lower())
            return OddsSnapshot(market=market, outcomes=converted) if converted else None
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
    raw_kind = _first(payload, "Type", "type", "kind", "messageType", "channel")
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
    # TxLINE PascalCase fields: Seq, Ts, FixtureId, StatusId, Clock, Type
    provider_sequence = _to_int(
        _first(payload, "Seq", "sequence", "seq", "sequenceNumber")
    )
    sequence = provider_sequence if provider_sequence is not None else fallback_sequence

    timestamp_ms = _to_int(
        _first(payload, "Ts", "timestamp", "ts", "timestampMs", "time")
    )
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    # TxLINE may send seconds; upscale to ms if the value looks like seconds.
    if timestamp_ms < 1_000_000_000_000:
        timestamp_ms *= 1000

    fixture_id = _to_int(_first(payload, "FixtureId", "fixture_id", "fixtureId", "fixture"))
    event_type = MatchEventType.from_raw(
        str(_first(payload, "Action", "event_type", "eventType", "event", "Type") or "").upper()
    )
    score = _extract_score(payload)
    odds = _extract_odds(payload)
    kind = _classify(payload, score, odds, event_type)

    return VerifiedFrame(
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload_hash=canonical_hash(payload),
        provider_sequence=provider_sequence,
        fixture_id=fixture_id,
        solana_validation_ref=solana_validation_ref,
        kind=kind,
        score=score,
        odds=odds,
        event_type=event_type,
        match_time=_extract_match_time(payload),
        status_id=_to_int(_first(payload, "StatusId", "status_id", "statusId", "status")),
        period=_to_int(_first(payload, "period", "phase")),
        raw=dict(payload),
    )


def _extract_match_time(payload: Mapping[str, Any]) -> Optional[str]:
    raw = _first(payload, "Clock", "match_time", "matchTime", "clock")
    if isinstance(raw, Mapping):
        seconds = _to_int(_first(raw, "Seconds", "seconds", "ElapsedTime"))
        if seconds is not None:
            return f"{seconds // 60}:{seconds % 60:02d}"
        return None
    return str(raw) if raw not in (None, "") else None
