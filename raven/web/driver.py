"""Deterministic web replay over real TxLINE scores, events, and consensus odds."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from raven.agent import RavenAgent, TickResult
from raven.feed.model import VerifiedFrame
from raven.feed.normalize import normalize
from raven.provenance.anchor import ArchiveAnchor
from raven.provenance.store import ReceiptEmitter
from raven.web.serialize import tick_to_json

_ROOT = Path(__file__).resolve().parents[2]
_SCORES_JSONL = _ROOT / "data/replay/scores_historical_18257739.jsonl"
_ODDS_JSONL = _ROOT / "data/replay/odds_historical_18257739.jsonl"
_RECEIPT_ARCHIVE = _ROOT / "receipts/anchored_demo.json"
_TXLINE_PROOF = _ROOT / "data/proofs/txline_score_18257739_seq1188.json"

_ACTION_TO_EVENT = {
    "goal": "GOAL",
    "red_card": "RED_CARD",
    "var": "VAR_OVERTURN",
}
_SHOCK_ACTIONS = frozenset(_ACTION_TO_EVENT)
_IMPORTANT_ACTIONS = _SHOCK_ACTIONS | {
    "game_finalised",
    "kickoff",
    "status",
    "score_adjustment",
    "action_amend",
}
_SAMPLE_INTERVAL_MS = 7_000
_TRANSITION_HOLD_SECONDS = {
    "WITHDRAW": 0.9,
    "HEDGE": 0.8,
    "RECALIBRATE": 0.7,
    "REENTER": 0.8,
}


def replay_path() -> str:
    return os.path.abspath(_SCORES_JSONL)


def odds_replay_path() -> str:
    return os.path.abspath(_ODDS_JSONL)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Replay data not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def _score_signature(record: dict[str, Any]) -> str:
    return json.dumps(
        {"action": record.get("Action"), "score": record.get("Score")},
        sort_keys=True,
        separators=(",", ":"),
    )


def _select_scores(
    records: list[dict[str, Any]], start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_sample = 0
    seen_shocks: set[str] = set()
    for record in records:
        timestamp = int(record.get("Ts", 0))
        action = str(record.get("Action") or "").lower()
        if not start_ms <= timestamp <= end_ms and action != "game_finalised":
            continue
        event_data = record.get("Data")
        is_enriched_event = (
            action not in {"goal", "red_card"}
            or (isinstance(event_data, dict) and bool(event_data.get("PlayerId")))
        )
        is_confirmed_shock = (
            action in _SHOCK_ACTIONS
            and bool(record.get("Confirmed", True))
            and is_enriched_event
        )
        if action in _SHOCK_ACTIONS and not is_confirmed_shock:
            continue
        if is_confirmed_shock:
            signature = _score_signature(record)
            if signature in seen_shocks:
                continue
            seen_shocks.add(signature)
        has_score = isinstance(record.get("Score"), dict)
        sampled = has_score and timestamp - last_sample >= _SAMPLE_INTERVAL_MS
        if is_confirmed_shock or action in (_IMPORTANT_ACTIONS - _SHOCK_ACTIONS) or sampled:
            selected.append(record)
            if sampled:
                last_sample = timestamp
    return selected


def _odds_market_key(record: dict[str, Any]) -> str:
    return (
        f"{record.get('MarketPeriod')}|{record.get('SuperOddsType')}|"
        f"{record.get('MarketParameters')}"
    )


def _select_odds(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_sample: dict[str, int] = {}
    last_prices: dict[str, tuple[Any, ...]] = {}
    for record in records:
        timestamp = int(record.get("Ts", 0))
        key = _odds_market_key(record)
        prices = tuple(record.get("Prices") or ())
        if not prices or prices == last_prices.get(key):
            continue
        if timestamp - last_sample.get(key, 0) < _SAMPLE_INTERVAL_MS:
            continue
        selected.append(record)
        last_sample[key] = timestamp
        last_prices[key] = prices
    return selected


def _combined_replay() -> list[tuple[int, int, dict[str, Any]]]:
    odds = _select_odds(_load_jsonl(_ODDS_JSONL))
    if not odds:
        raise ValueError("Real TxLINE odds replay is empty")
    start_ms = min(int(record["Ts"]) for record in odds)
    end_ms = max(int(record["Ts"]) for record in odds)
    scores = _select_scores(_load_jsonl(_SCORES_JSONL), start_ms, end_ms)
    combined = [
        (int(record.get("Ts", 0)), 0, record) for record in scores
    ] + [
        (int(record.get("Ts", 0)), 1, record) for record in odds
    ]
    return sorted(combined, key=lambda item: (item[0], item[1]))


def _prepare_payload(record: dict[str, Any], kind: int) -> dict[str, Any]:
    payload = dict(record)
    if kind == 1:
        payload["type"] = "odds"
        return payload
    action = str(payload.get("Action") or "").lower()
    if action in _ACTION_TO_EVENT:
        payload["event_type"] = _ACTION_TO_EVENT[action]
        payload["type"] = "event"
    elif action == "game_finalised":
        payload["event_type"] = "GAME_FINALISED"
        payload["type"] = "event"
    else:
        payload["type"] = "score"
    return payload


def iter_verified_frames() -> Iterator[VerifiedFrame]:
    """Yield the merged historical match through the production normalizer."""
    validation_ref = None
    proof_fixture = None
    proof_sequence = None
    if _TXLINE_PROOF.exists():
        proof = json.loads(_TXLINE_PROOF.read_text(encoding="utf-8"))
        proof_fixture = int(proof["fixtureId"])
        proof_sequence = int(proof["sequence"])
        validation_ref = (
            f"solana-devnet:{proof['programId']}:{proof['fixtureId']}:{proof['sequence']}"
        )
    for tick_index, (_, kind, raw_record) in enumerate(_combined_replay(), start=1):
        is_proven_score = (
            proof_fixture is not None
            and int(raw_record.get("FixtureId", 0)) == proof_fixture
            and int(raw_record.get("Seq", -1)) == proof_sequence
        )
        frame = normalize(
            _prepare_payload(raw_record, kind),
            fallback_sequence=tick_index,
            solana_validation_ref=validation_ref if is_proven_score else None,
        )
        # Native score sequences and historical odds fallback sequences belong
        # to different domains. Keep replay ordering monotonic while retaining
        # the provider sequence separately for provenance and receipts.
        yield replace(frame, sequence=tick_index)


def run_replay(
    *,
    speed: float = 12.0,
    max_ticks: Optional[int] = None,
    agent: Optional[RavenAgent] = None,
) -> Iterator[Dict[str, Any]]:
    """Run real TxLINE records through the production decision core."""
    raven = agent or RavenAgent(
        emitter=ReceiptEmitter(anchor=ArchiveAnchor(_RECEIPT_ARCHIVE))
    )
    tick_index = 0
    frame_delay = (1.0 / speed) if speed and speed > 0 else 0.0

    for frame in iter_verified_frames():
        result: TickResult = raven.on_frame(frame)
        tick_index += 1
        yield tick_to_json(
            result,
            tick_index=tick_index,
            inventory=raven.inventory,
            match_state=raven.state,
            market_odds=raven.odds,
        )
        if max_ticks is not None and tick_index >= max_ticks:
            break
        if frame_delay:
            hold = (
                _TRANSITION_HOLD_SECONDS.get(result.state.value, 0.0)
                if result.risk.transitioned
                else 0.0
            )
            time.sleep(frame_delay + hold)


def summary(agent_results: list) -> Dict[str, Any]:
    total_pnl = sum(result.realized_spread_pnl for result in agent_results)
    receipts = sum(1 for result in agent_results if result.receipt is not None)
    return {"ticks": len(agent_results), "spread_pnl": total_pnl, "receipts": receipts}
