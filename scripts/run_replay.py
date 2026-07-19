#!/usr/bin/env python3
"""Run RAVEN agent against real TxLINE JSONL for fixture 18222446.

The scores/historical endpoint carries real match events (goals, shots,
red cards, etc.) with authentic Seq numbers 0..1306 but no odds field.
This script bridges the gap — it maps TxLINE Action → event_type so
RAVEN's normalizer understands them, and derives synthetic-but-realistic
odds from the live match state. Real provenance (Seq, fixture_id,
match_time, score) + real events driving the odds model.
"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from raven.agent import RavenAgent, TickResult
from raven.feed.normalize import normalize

JSONL = os.path.join(
    os.path.dirname(__file__), "..", "data", "replay",
    "scores_historical_18222446.jsonl",
)

# TxLINE Action → RAVEN MatchEventType name.
# MatchEventType.from_raw() does: key = value.strip().upper().replace(" ","_")
# then tries cls(key). So we must use the exact enum value strings.
ACTION_TO_EVENT = {
    "goal":      "GOAL",             # → MatchEventType.GOAL      (is_shock=True)
    "red_card":  "RED_CARD",         # → MatchEventType.RED_CARD  (is_shock=True)
    "var":       "VAR_OVERTURN",     # → MatchEventType.VAR_OVERTURN (is_shock=True)
}

# Actions that classify as shock frames (force WITHDRAW via FR4.2 hard reflex)
SHOCK_ACTIONS = {"goal", "red_card", "var"}


def make_odds(home_goals: int, away_goals: int, minute: int, shock: bool) -> dict:
    """Generate plausible 1X2 odds from match state.

    Pre-shock odds drift naturally with score/time.
    Post-shock odds jump sharply.
    """
    # Base implied probs from score difference
    diff = home_goals - away_goals
    time_factor = max(0.0, 1.0 - minute / 95.0)  # urgency of trailing team

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

    # Clamp
    p_home = max(0.05, min(0.88, p_home))
    p_draw = max(0.05, min(0.35, p_draw))
    p_away = max(0.05, min(0.88, p_away))

    # Shock: compress the winner's price (favourite shortens sharply)
    if shock:
        if diff >= 0:
            p_home = min(0.92, p_home * 1.25)
        else:
            p_away = min(0.92, p_away * 1.25)
        total = p_home + p_draw + p_away
        p_home /= total; p_draw /= total; p_away /= total

    # Convert to decimal odds with a 5% vig
    vig = 0.95
    return {
        "home": round(vig / p_home, 2),
        "draw": round(vig / p_draw, 2),
        "away": round(vig / p_away, 2),
    }


def _render(result: TickResult) -> None:
    f = result.frame
    tag = result.state.value.ljust(11)
    seq = f"seq #{f.sequence}".ljust(14)
    extra = ""
    if result.hedge is not None and not result.hedge.is_noop:
        extra = f"  hedge: {len(result.hedge.trades)} trade(s)"
    elif result.is_quoting:
        extra = f"  spread P&L +{result.realized_spread_pnl:.4f}"
    recv = "  [receipt ✓]" if result.receipt is not None else ""
    print(f"{tag} {seq} {result.risk.reason}{extra}{recv}")


def main() -> int:
    path = os.path.abspath(JSONL)
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        return 1

    print("RAVEN real-data replay — fixture 18222446  Seq 0..1306")
    print(f"Source: {path}")
    print("-" * 68)

    agent = RavenAgent(on_tick=_render)

    home_goals = away_goals = 0
    clock_minute = 0
    shock_cooldown = 0  # frames until shock odds decay

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

            # Extract match clock (seconds → minutes)
            elapsed = data.get("ElapsedTime") or data.get("elapsed_time")
            if elapsed is not None:
                try:
                    clock_minute = int(elapsed) // 60
                except (TypeError, ValueError):
                    pass

            # Extract score
            score_data = data.get("Score") or {}
            if isinstance(score_data, dict):
                try:
                    home_goals = int(score_data.get("Home", home_goals))
                    away_goals = int(score_data.get("Away", away_goals))
                except (TypeError, ValueError):
                    pass

            # Determine if this is a shock frame
            is_shock = action in SHOCK_ACTIONS
            if is_shock:
                shock_cooldown = 6   # next 6 frames also use shock odds

            use_shock_odds = is_shock or shock_cooldown > 0
            if shock_cooldown > 0 and not is_shock:
                shock_cooldown -= 1

            # Build an enriched payload the normalizer can work with
            enriched = dict(record)
            enriched["seq"] = seq           # lowercase alias for normalizer
            enriched["fixture_id"] = record.get("FixtureId", 18222446)
            enriched["match_time"] = f"{clock_minute}:00"
            enriched["score"] = {"home": home_goals, "away": away_goals}

            # Shock frames: type=event so _classify() returns FrameKind.EVENT
            # and frame.is_shock fires the FR4.2 hard-reflex WITHDRAW.
            # Non-shock frames: type=odds so the pricing layer processes them.
            if is_shock and action in ACTION_TO_EVENT:
                enriched["event_type"] = ACTION_TO_EVENT[action]
                enriched["type"] = "event"
                # No odds on pure event frames — let RAVEN use last price.
            else:
                if action in ACTION_TO_EVENT:
                    enriched["event_type"] = ACTION_TO_EVENT[action]
                enriched["type"] = "odds"
                enriched["odds"] = make_odds(home_goals, away_goals,
                                             clock_minute, use_shock_odds)

            frame = normalize(enriched, fallback_sequence=lineno)
            agent.on_frame(frame)

    total_pnl = sum(r.realized_spread_pnl for r in agent.results)
    receipts = sum(1 for r in agent.results if r.receipt is not None)
    n = len(agent.results)
    print("-" * 68)
    print(f"ticks={n}  spread P&L={total_pnl:.4f}  receipts anchored={receipts}")
    print(f"States seen: {', '.join(sorted({r.state.value for r in agent.results}))}")
    transitions = [(r.prior_state.value, r.state.value)
                   for r in agent.results if r.risk.transitioned]
    if transitions:
        print("State transitions:")
        for p, s in transitions:
            print(f"  {p} -> {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
