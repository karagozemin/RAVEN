"""Replay driver for the Web Control Room.

Runs the full :class:`~raven.agent.RavenAgent` against the *real* captured
TxLINE feed for fixture 18222446 and yields one serialized tick dict per frame.
This is the same integration used by ``scripts/run_replay.py`` — the historical
scores endpoint carries authentic Seq numbers, scores and match events but no
odds field, so odds are derived deterministically from the live match state
(exactly as in the CLI replay) while all provenance stays real.

The driver is transport-agnostic: it just produces JSON-safe tick dicts. The
server decides how to push them to the browser (SSE here).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterator, Optional

from raven.agent import RavenAgent, TickResult
from raven.feed.normalize import normalize
from raven.web.serialize import tick_to_json

_REPLAY_JSONL = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "data",
    "replay",
    "scores_historical_18222446.jsonl",
)

# TxLINE Action -> RAVEN MatchEventType value string.
_ACTION_TO_EVENT = {
    "goal": "GOAL",
    "red_card": "RED_CARD",
    "var": "VAR_OVERTURN",
}
_SHOCK_ACTIONS = {"goal", "red_card", "var"}


def _extract_minute(record: Dict[str, Any], data: Dict[str, Any]) -> Optional[int]:
    """Real TxLINE carries the clock top-level as ``Clock.Seconds``.

    We also tolerate the older ``Data.ElapsedTime`` shape for safety.
    """
    clock = record.get("Clock")
    if isinstance(clock, dict):
        secs = clock.get("Seconds")
        try:
            if secs is not None:
                return int(secs) // 60
        except (TypeError, ValueError):
            pass
    elapsed = data.get("ElapsedTime") or data.get("elapsed_time")
    try:
        if elapsed is not None:
            return int(elapsed) // 60
    except (TypeError, ValueError):
        pass
    return None


def _participant_goals(score: Dict[str, Any], key: str) -> Optional[int]:
    part = score.get(key)
    if not isinstance(part, dict):
        return None
    total = part.get("Total") or part.get("HT") or part.get("H1")
    if isinstance(total, dict):
        try:
            return int(total.get("Goals"))
        except (TypeError, ValueError):
            return None
    return None



def _make_odds(home: int, away: int, minute: int, shock: bool) -> Dict[str, float]:
    """Derive plausible 1X2 decimal odds from match state (see run_replay)."""
    diff = home - away
    time_factor = max(0.0, 1.0 - minute / 95.0)

    if diff > 0:
        p_home = 0.70 + 0.05 * diff - 0.02 * (1 - time_factor)
        p_draw = 0.18 - 0.03 * diff
        p_away = 1.0 - p_home - p_draw
    elif diff < 0:
        p_away = 0.70 + 0.05 * (-diff) - 0.02 * (1 - time_factor)
        p_draw = 0.18 - 0.03 * (-diff)
        p_home = 1.0 - p_away - p_draw
    else:
        p_home = 0.38 + 0.05 * (1 - time_factor)
        p_draw = 0.28 - 0.02 * (1 - time_factor)
        p_away = 1.0 - p_home - p_draw

    p_home = max(0.05, min(0.88, p_home))
    p_draw = max(0.05, min(0.35, p_draw))
    p_away = max(0.05, min(0.88, p_away))

    if shock:
        if diff >= 0:
            p_home = min(0.92, p_home * 1.25)
        else:
            p_away = min(0.92, p_away * 1.25)
        total = p_home + p_draw + p_away
        p_home /= total
        p_draw /= total
        p_away /= total

    vig = 0.95
    return {
        "home": round(vig / p_home, 2),
        "draw": round(vig / p_draw, 2),
        "away": round(vig / p_away, 2),
    }


def replay_path() -> str:
    return os.path.abspath(_REPLAY_JSONL)


def run_replay(
    *,
    speed: float = 12.0,
    max_ticks: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield serialized tick dicts as the agent processes the real feed.

    ``speed`` throttles playback so the browser animation is watchable — it is
    the number of frames emitted per second (a plain sleep between ticks, since
    the historical scores file has no usable inter-frame timing). ``max_ticks``
    caps the run for smoke tests.
    """
    path = replay_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"Replay data not found: {path}")

    agent = RavenAgent()
    home_goals = away_goals = 0
    clock_minute = 0
    shock_cooldown = 0
    tick_index = 0
    frame_delay = (1.0 / speed) if speed and speed > 0 else 0.0

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = record.get("Action", "")
            data = record.get("Data") or {}
            seq = record.get("Seq", lineno)

            # Real match clock: TxLINE sends it top-level as Clock.Seconds.
            minute = _extract_minute(record, data)
            if minute is not None:
                clock_minute = minute

            # Real score: nested under Score.Participant{1,2}.Total.Goals.
            # (Participant1IsHome tells us which side is "home".)
            score_data = record.get("Score") or {}
            if isinstance(score_data, dict) and score_data:
                p1_home = bool(record.get("Participant1IsHome", True))
                g1 = _participant_goals(score_data, "Participant1")
                g2 = _participant_goals(score_data, "Participant2")
                home_raw = g1 if p1_home else g2
                away_raw = g2 if p1_home else g1
                if home_raw is not None:
                    home_goals = home_raw
                if away_raw is not None:
                    away_goals = away_raw


            is_shock = action in _SHOCK_ACTIONS
            if is_shock:
                shock_cooldown = 6
            use_shock_odds = is_shock or shock_cooldown > 0
            if shock_cooldown > 0 and not is_shock:
                shock_cooldown -= 1

            enriched = dict(record)
            # Drop the raw Clock dict so the normalizer picks up our formatted
            # "MM:00" match_time string instead of stringifying the dict.
            enriched.pop("Clock", None)
            enriched["seq"] = seq

            enriched["fixture_id"] = record.get("FixtureId", 18222446)
            enriched["match_time"] = f"{clock_minute}:00"
            enriched["score"] = {"home": home_goals, "away": away_goals}

            if is_shock and action in _ACTION_TO_EVENT:
                enriched["event_type"] = _ACTION_TO_EVENT[action]
                enriched["type"] = "event"
            else:
                if action in _ACTION_TO_EVENT:
                    enriched["event_type"] = _ACTION_TO_EVENT[action]
                enriched["type"] = "odds"
                enriched["odds"] = _make_odds(
                    home_goals, away_goals, clock_minute, use_shock_odds
                )

            frame = normalize(enriched, fallback_sequence=lineno)
            result: TickResult = agent.on_frame(frame)
            tick_index += 1

            yield tick_to_json(
                result, tick_index=tick_index, inventory=agent.inventory
            )

            if max_ticks is not None and tick_index >= max_ticks:
                break
            if frame_delay:
                time.sleep(frame_delay)


def summary(agent_results: list) -> Dict[str, Any]:  # pragma: no cover - helper
    """Aggregate summary stats (unused by SSE; handy for tests)."""
    total_pnl = sum(r.realized_spread_pnl for r in agent_results)
    receipts = sum(1 for r in agent_results if r.receipt is not None)
    return {"ticks": len(agent_results), "spread_pnl": total_pnl, "receipts": receipts}
