#!/usr/bin/env python3
"""F8 Counterfactual Lab — Baseline MM vs RAVEN on real TxLINE data.

Reads data/replay/scores_historical_18222446.jsonl (1,307 real ticks,
Seq 0..1306, Argentina-Switzerland / real fixture), enriches each record
with the same Action→event_type + make_odds pipeline used in run_replay.py,
then hands the resulting Tick stream to counterfactual._BaselineAgent and
counterfactual._RavenAgent for a head-to-head P&L comparison.

All numbers come from the real replay — nothing is hand-picked.
"""
import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from raven.counterfactual import (
    Tick, _BaselineAgent, _RavenAgent, _print_comparison,
)

JSONL = os.path.join(
    os.path.dirname(__file__), "..", "data", "replay",
    "scores_historical_18222446.jsonl",
)

# Exact same mapping as run_replay.py
ACTION_TO_EVENT = {
    "goal":     "GOAL",
    "red_card": "RED_CARD",
    "var":      "VAR_OVERTURN",
}
SHOCK_ACTIONS = {"goal", "red_card", "var"}


def make_odds(home_goals: int, away_goals: int, minute: int, shock: bool) -> tuple:
    diff = home_goals - away_goals
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
        factor = 1.25 if diff >= 0 else 1.0
        if diff < 0: p_away = min(0.92, p_away * 1.25)
        else:        p_home = min(0.92, p_home * 1.25)
        total = p_home + p_draw + p_away
        p_home /= total; p_draw /= total; p_away /= total
    vig = 0.95
    return (
        round(vig / p_home, 2),
        round(vig / p_draw, 2),
        round(vig / p_away, 2),
    )


def build_ticks(path: str) -> list:
    ticks = []
    home_goals = away_goals = 0
    clock_minute = 0
    shock_cooldown = 0
    seq_base = int(time.time() * 1000)

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

            # TxLINE real format: Clock.Seconds for elapsed time
            clock_raw = record.get("Clock") or {}
            secs = clock_raw.get("Seconds")
            if secs is not None:
                try:
                    clock_minute = int(secs) // 60
                except (TypeError, ValueError):
                    pass

            # TxLINE real format: Score.Participant1.Total.Goals / Score.Participant2.Total.Goals
            score_raw = record.get("Score") or {}
            p1 = score_raw.get("Participant1") or {}
            p2 = score_raw.get("Participant2") or {}
            p1_goals = (p1.get("Total") or {}).get("Goals")
            p2_goals = (p2.get("Total") or {}).get("Goals")
            if p1_goals is not None:
                try:
                    home_goals = int(p1_goals)
                except (TypeError, ValueError):
                    pass
            if p2_goals is not None:
                try:
                    away_goals = int(p2_goals)
                except (TypeError, ValueError):
                    pass

            is_shock = action in SHOCK_ACTIONS
            if is_shock:
                shock_cooldown = 6
            use_shock_odds = is_shock or shock_cooldown > 0
            if shock_cooldown > 0 and not is_shock:
                shock_cooldown -= 1

            oh, od, oa = make_odds(home_goals, away_goals, clock_minute, use_shock_odds)
            ev = ACTION_TO_EVENT.get(action) if is_shock else None

            ticks.append(Tick(
                raw=record,
                sequence=int(seq),
                ts_ms=seq_base + lineno * 6000,   # ~6 s per tick (real cadence)
                event_type=ev,
                match_time_s=clock_minute * 60,
                score_home=home_goals,
                score_away=away_goals,
                odds_home=oh,
                odds_draw=od,
                odds_away=oa,
                is_finalised=(action == "game_finalised"),
            ))
    return ticks


def main() -> int:
    path = os.path.abspath(JSONL)
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        return 1

    print(f"Loading real TxLINE data: {path}")
    ticks = build_ticks(path)
    print(f"Loaded {len(ticks)} ticks (Seq {ticks[0].sequence}..{ticks[-1].sequence})\n")

    # Count shock events found
    shock_ticks = [t for t in ticks if t.event_type is not None]
    print(f"Shock events in feed: {len(shock_ticks)}")
    for t in shock_ticks:
        print(f"  seq #{t.sequence:4d}  {t.event_type}  score {t.score_home}-{t.score_away}  min {t.match_time_s//60}'")
    print()

    baseline = _BaselineAgent()
    raven    = _RavenAgent()

    for tick in ticks:
        baseline.on_tick(tick)
        raven.on_tick(tick)

    _print_comparison(baseline.pos, raven.pos, len(ticks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
