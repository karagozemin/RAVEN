"""Regression tests for raven/feed/normalize.py.

Covers the two bugs fixed on 2026-07-19:
  1. "Type" field overriding "Action" — e.g. Type="Soccer" silently
     replaced Action="goal", producing event_type=OTHER instead of GOAL.
  2. Raw action value not uppercased before MatchEventType.from_raw(),
     so lowercase "goal" / "yellow_card" etc. were not matched.
"""

import pytest
from raven.feed.model import MatchEventType, FrameKind
from raven.feed.normalize import normalize


# ---------------------------------------------------------------------------
# Minimal TxLINE-style payload (mirrors the real scores_historical format)
# ---------------------------------------------------------------------------

BASE = {
    "FixtureId": 18222446,
    "Seq": 116,
    "Ts": 1783818619532,
    "StatusId": 2,
    "Type": "Soccer",          # <-- real TxLINE always sends this
    "GameState": "inprogress",
}


def frame(overrides: dict):
    raw = {**BASE, **overrides}
    return normalize(raw, fallback_sequence=1)


# ---------------------------------------------------------------------------
# Bug 1 + 2 combined: Type="Soccer" must NOT override Action="goal"
# ---------------------------------------------------------------------------

class TestActionTakesPrecedenceOverType:

    def test_goal_action_gives_goal_event_type(self):
        f = frame({"Action": "goal"})
        assert f.event_type == MatchEventType.GOAL, (
            f"Expected GOAL, got {f.event_type}. "
            "'Type: Soccer' is overriding 'Action: goal'."
        )

    def test_goal_action_gives_event_kind(self):
        f = frame({"Action": "goal"})
        assert f.kind == FrameKind.EVENT

    def test_goal_action_is_shock(self):
        f = frame({"Action": "goal"})
        assert f.is_shock, "GOAL frames must be classified as shocks"

    def test_red_card_action_is_shock(self):
        f = frame({"Action": "red_card"})
        assert f.event_type == MatchEventType.RED_CARD
        assert f.is_shock

    def test_yellow_card_action_not_shock(self):
        # yellow cards are not in the shock set (per model.py)
        f = frame({"Action": "yellow_card"})
        # may be OTHER or YELLOW_CARD depending on model — just must not be GOAL/RED_CARD
        assert f.event_type not in (MatchEventType.GOAL, MatchEventType.RED_CARD)
        assert not f.is_shock

    def test_possession_action_not_shock(self):
        f = frame({"Action": "attack_possession"})
        assert not f.is_shock


# ---------------------------------------------------------------------------
# Bug 2: uppercase normalisation
# ---------------------------------------------------------------------------

class TestUppercaseNormalisation:

    @pytest.mark.parametrize("raw_action", ["goal", "GOAL", "Goal"])
    def test_case_insensitive_goal(self, raw_action):
        f = frame({"Action": raw_action})
        assert f.event_type == MatchEventType.GOAL, (
            f"Action={raw_action!r} should parse as GOAL"
        )

    @pytest.mark.parametrize("raw_action", ["red_card", "RED_CARD", "Red_Card"])
    def test_case_insensitive_red_card(self, raw_action):
        f = frame({"Action": raw_action})
        assert f.event_type == MatchEventType.RED_CARD

    def test_game_finalised_case_insensitive(self):
        f = frame({"Action": "game_finalised"})
        assert f.event_type == MatchEventType.GAME_FINALISED


# ---------------------------------------------------------------------------
# Sequence + timestamp passthrough
# ---------------------------------------------------------------------------

class TestFieldPassthrough:

    def test_seq_extracted(self):
        f = frame({"Action": "goal", "Seq": 999})
        assert f.sequence == 999

    def test_fallback_sequence_used_when_missing(self):
        raw = {**BASE}
        del raw["Seq"]
        raw["Action"] = "goal"
        f = normalize(raw, fallback_sequence=42)
        assert f.sequence == 42

    def test_fixture_id(self):
        f = frame({"Action": "goal"})
        assert f.fixture_id == 18222446

    def test_timestamp_ms_upscaled_from_seconds(self):
        # Ts < 1e12 should be multiplied by 1000
        f = normalize({**BASE, "Action": "goal", "Ts": 1_783_818_619}, fallback_sequence=1)
        assert f.timestamp_ms == 1_783_818_619_000
