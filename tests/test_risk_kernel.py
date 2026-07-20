"""Deterministic state-machine tests for RAVEN's Risk Kernel (F4).

These tests are the contract for the behavior the demo's "winner moment" hinges
on: a verified shock must force an immediate WITHDRAW regardless of score, the
kernel must then sequence HEDGE -> RECALIBRATE -> REENTER -> NORMAL, and the
whole thing must be perfectly deterministic so it can be replayed (F8) and
anchored (F7).

Run with:  pytest tests/test_risk_kernel.py -q
"""

from __future__ import annotations

import pytest

from raven.feed.model import (
    FrameKind,
    MatchEventType,
    VerifiedFrame,
)
from raven.risk.kernel import (
    RiskDecision,
    RiskKernel,
    RiskSignals,
    RiskState,
    RiskWeights,
)


# --------------------------------------------------------------------------- #
# Frame builders
# --------------------------------------------------------------------------- #

_SEQ = {"n": 0}


def _next_seq() -> int:
    _SEQ["n"] += 1
    return _SEQ["n"]


def calm_frame(**overrides) -> VerifiedFrame:
    """A benign, non-shock update frame (an odds/score refresh)."""
    seq = overrides.pop("sequence", _next_seq())
    return VerifiedFrame(
        sequence=seq,
        timestamp_ms=1_784_412_000_000 + seq,
        payload_hash=f"hash-{seq}",
        fixture_id=17952170,
        kind=overrides.pop("kind", FrameKind.ODDS),
        event_type=overrides.pop("event_type", MatchEventType.OTHER),
        **overrides,
    )


def shock_frame(
    event_type: MatchEventType = MatchEventType.PENALTY_AWARDED,
    **overrides,
) -> VerifiedFrame:
    """A verified shock event frame (the FR4.2 hard trigger)."""
    seq = overrides.pop("sequence", _next_seq())
    return VerifiedFrame(
        sequence=seq,
        timestamp_ms=1_784_412_000_000 + seq,
        payload_hash=f"hash-{seq}",
        fixture_id=17952170,
        kind=FrameKind.EVENT,
        event_type=event_type,
        **overrides,
    )


CALM = RiskSignals()  # all zeros -> score 0.0
ELEVATED = RiskSignals(event_latency=1.0)  # 0.25 -> just under caution? no, ==
HIGH = RiskSignals(
    consensus_dev=1.0,
    event_latency=1.0,
    cross_market_incoherence=1.0,
)  # 0.75 -> above withdraw


# --------------------------------------------------------------------------- #
# RiskWeights / RiskSignals unit behavior
# --------------------------------------------------------------------------- #


def test_default_weights_sum_to_one():
    RiskWeights().validate()  # must not raise


def test_weights_that_do_not_sum_to_one_raise():
    with pytest.raises(ValueError):
        RiskWeights(consensus_dev=0.5).validate()


def test_score_is_bounded_in_unit_interval():
    # Even with out-of-range inputs, clipping keeps the score in [0, 1].
    s = RiskSignals(
        consensus_dev=5.0,
        event_latency=-2.0,
        cross_market_incoherence=1.0,
        exposure=1.0,
        feed_confidence=1.0,
    )
    score = s.score(RiskWeights())
    assert 0.0 <= score <= 1.0


def test_score_matches_prd_weighting():
    s = RiskSignals(
        consensus_dev=1.0,
        event_latency=1.0,
        cross_market_incoherence=1.0,
        exposure=1.0,
        feed_confidence=1.0,
    )
    # All ones -> score equals the sum of weights == 1.0
    assert s.score(RiskWeights()) == pytest.approx(1.0)


def test_settle_pressure_excludes_exposure_and_consensus_dev():
    """RECALIBRATE stability must ignore structurally-elevated post-shock terms.

    exposure (residual hedge) and consensus_dev (fair value pinned at the
    model-risk cap) are expected to be high right after a shock and must not
    block re-entry.
    """
    s = RiskSignals(
        consensus_dev=1.0,   # excluded
        exposure=1.0,        # excluded
        event_latency=0.1,
        cross_market_incoherence=0.2,
        feed_confidence=0.05,
    )
    # settle_pressure is the loudest of the three settle signals only.
    assert s.settle_pressure() == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# Baseline posture
# --------------------------------------------------------------------------- #


def test_starts_in_normal():
    k = RiskKernel()
    assert k.state is RiskState.NORMAL


def test_calm_frame_stays_normal():
    k = RiskKernel()
    d = k.observe(calm_frame(), CALM)
    assert d.state is RiskState.NORMAL
    assert not d.transitioned
    assert not d.should_withdraw


# --------------------------------------------------------------------------- #
# Score-driven bands (NORMAL / CAUTION / WITHDRAW)
# --------------------------------------------------------------------------- #


def test_normal_to_caution_on_elevated_score():
    k = RiskKernel(caution_threshold=0.35, withdraw_threshold=0.65)
    # score 0.40 -> between caution and withdraw
    signals = RiskSignals(consensus_dev=1.0, event_latency=0.4)
    d = k.observe(calm_frame(), signals)
    assert d.state is RiskState.CAUTION


def test_caution_back_to_normal_when_risk_subsides():
    k = RiskKernel()
    k.observe(calm_frame(), RiskSignals(consensus_dev=1.0, event_latency=0.4))
    assert k.state is RiskState.CAUTION
    d = k.observe(calm_frame(), CALM)
    assert d.state is RiskState.NORMAL


def test_normal_to_withdraw_on_high_score_without_shock():
    k = RiskKernel()
    d = k.observe(calm_frame(), HIGH)
    assert d.state is RiskState.WITHDRAW
    assert d.should_withdraw


# --------------------------------------------------------------------------- #
# The reflex: a verified shock forces WITHDRAW regardless of score (FR4.2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "event_type",
    [
        MatchEventType.GOAL,
        MatchEventType.RED_CARD,
        MatchEventType.PENALTY_AWARDED,
        MatchEventType.VAR_OVERTURN,
    ],
)
def test_shock_forces_withdraw_from_normal_even_with_calm_score(event_type):
    k = RiskKernel()
    d = k.observe(shock_frame(event_type), CALM)
    assert d.state is RiskState.WITHDRAW
    assert d.triggered_by_shock
    assert d.should_withdraw
    assert event_type.value in d.reason


def test_shock_forces_withdraw_from_caution():
    k = RiskKernel()
    k.observe(calm_frame(), RiskSignals(consensus_dev=1.0, event_latency=0.4))
    assert k.state is RiskState.CAUTION
    d = k.observe(shock_frame(), CALM)
    assert d.state is RiskState.WITHDRAW


def test_non_shock_event_does_not_trigger_reflex():
    """A VAR_REVIEW (not an overturn) is informational, not a shock."""
    k = RiskKernel()
    d = k.observe(shock_frame(MatchEventType.VAR_REVIEW), CALM)
    assert d.state is RiskState.NORMAL
    assert not d.triggered_by_shock


# --------------------------------------------------------------------------- #
# The sequenced recovery: WITHDRAW -> HEDGE -> RECALIBRATE -> REENTER -> NORMAL
# --------------------------------------------------------------------------- #


def test_withdraw_advances_to_hedge():
    k = RiskKernel()
    k.observe(shock_frame(), CALM)  # -> WITHDRAW
    d = k.observe(calm_frame(), CALM)  # WITHDRAW always -> HEDGE
    assert d.state is RiskState.HEDGE


def test_hedge_holds_until_hedge_complete():
    k = RiskKernel()
    k.observe(shock_frame(), CALM)      # WITHDRAW
    k.observe(calm_frame(), CALM)       # HEDGE
    d = k.observe(calm_frame(), CALM, hedge_complete=False)
    assert d.state is RiskState.HEDGE
    d = k.observe(calm_frame(), CALM, hedge_complete=True)
    assert d.state is RiskState.RECALIBRATE


def test_recalibrate_requires_n_stable_updates_then_reenters():
    k = RiskKernel(stable_updates_required=3)
    k.observe(shock_frame(), CALM)                 # WITHDRAW
    k.observe(calm_frame(), CALM)                  # HEDGE
    k.observe(calm_frame(), CALM, hedge_complete=True)  # RECALIBRATE
    # Three consecutive settled, shock-free updates required.
    d1 = k.observe(calm_frame(), CALM)
    assert d1.state is RiskState.RECALIBRATE
    d2 = k.observe(calm_frame(), CALM)
    assert d2.state is RiskState.RECALIBRATE
    d3 = k.observe(calm_frame(), CALM)
    assert d3.state is RiskState.REENTER


def test_recalibrate_survives_structurally_elevated_post_shock_risk():
    """High exposure + consensus_dev must NOT block re-entry.

    This is the trap the settle_pressure design avoids: the full blended score
    is still high after a hedge, but the feed/coherence signals are calm, so
    RECALIBRATE should still count these ticks as stable.
    """
    k = RiskKernel(stable_updates_required=2)
    k.observe(shock_frame(), CALM)                 # WITHDRAW
    k.observe(calm_frame(), CALM)                  # HEDGE
    post_shock = RiskSignals(consensus_dev=1.0, exposure=1.0)  # score 0.45
    k.observe(calm_frame(), post_shock, hedge_complete=True)   # RECALIBRATE
    k.observe(calm_frame(), post_shock)            # stable 1
    d = k.observe(calm_frame(), post_shock)        # stable 2 -> REENTER
    assert d.state is RiskState.REENTER


def test_recalibrate_counter_resets_on_unsettled_feed():
    k = RiskKernel(stable_updates_required=2)
    k.observe(shock_frame(), CALM)                 # WITHDRAW
    k.observe(calm_frame(), CALM)                  # HEDGE
    k.observe(calm_frame(), CALM, hedge_complete=True)  # RECALIBRATE
    k.observe(calm_frame(), CALM)                  # stable 1
    # An unsettled feed (high latency) resets the stability counter.
    k.observe(calm_frame(), RiskSignals(event_latency=1.0))
    k.observe(calm_frame(), CALM)                  # stable 1 again
    d = k.observe(calm_frame(), CALM)              # stable 2 -> REENTER
    assert d.state is RiskState.REENTER


def test_recalibrate_counter_resets_on_second_shock():
    k = RiskKernel(stable_updates_required=2)
    k.observe(shock_frame(), CALM)                 # WITHDRAW
    k.observe(calm_frame(), CALM)                  # HEDGE
    k.observe(calm_frame(), CALM, hedge_complete=True)  # RECALIBRATE
    k.observe(calm_frame(), CALM)                  # stable 1
    # A fresh shock during recalibration snaps back to WITHDRAW.
    d = k.observe(shock_frame(MatchEventType.GOAL), CALM)
    assert d.state is RiskState.WITHDRAW


def test_reenter_to_normal_with_hysteresis():
    k = RiskKernel(
        caution_threshold=0.35,
        reenter_relief=0.10,
        stable_updates_required=1,
    )
    k.observe(shock_frame(), CALM)                 # WITHDRAW
    k.observe(calm_frame(), CALM)                  # HEDGE
    k.observe(calm_frame(), CALM, hedge_complete=True)  # RECALIBRATE
    k.observe(calm_frame(), CALM)                  # -> REENTER
    assert k.state is RiskState.REENTER
    # Needs score <= caution - relief = 0.25 to fall back to NORMAL.
    d = k.observe(calm_frame(), CALM)              # score 0.0
    assert d.state is RiskState.NORMAL


def test_reenter_holds_when_within_hysteresis_band():
    k = RiskKernel(
        caution_threshold=0.35,
        reenter_relief=0.10,
        stable_updates_required=1,
    )
    k.observe(shock_frame(), CALM)
    k.observe(calm_frame(), CALM)
    k.observe(calm_frame(), CALM, hedge_complete=True)
    k.observe(calm_frame(), CALM)                  # REENTER
    # score 0.30: below caution(0.35) but above caution-relief(0.25) -> hold.
    d = k.observe(calm_frame(), RiskSignals(consensus_dev=1.0, event_latency=0.3))
    assert d.state is RiskState.REENTER


def test_reenter_spikes_back_to_withdraw():
    k = RiskKernel(stable_updates_required=1)
    k.observe(shock_frame(), CALM)
    k.observe(calm_frame(), CALM)
    k.observe(calm_frame(), CALM, hedge_complete=True)
    k.observe(calm_frame(), CALM)                  # REENTER
    d = k.observe(calm_frame(), HIGH)              # score spikes -> WITHDRAW
    assert d.state is RiskState.WITHDRAW


# --------------------------------------------------------------------------- #
# Full lifecycle + provenance + determinism
# --------------------------------------------------------------------------- #


def test_full_lifecycle_sequence():
    k = RiskKernel(stable_updates_required=3)
    states = []
    states.append(k.observe(calm_frame(), CALM).state)            # NORMAL
    states.append(k.observe(shock_frame(), CALM).state)           # WITHDRAW
    states.append(k.observe(calm_frame(), CALM).state)            # HEDGE
    states.append(
        k.observe(calm_frame(), CALM, hedge_complete=True).state  # RECALIBRATE
    )
    states.append(k.observe(calm_frame(), CALM).state)            # RECALIBRATE
    states.append(k.observe(calm_frame(), CALM).state)            # RECALIBRATE
    states.append(k.observe(calm_frame(), CALM).state)            # REENTER
    states.append(k.observe(calm_frame(), CALM).state)            # NORMAL
    assert states == [
        RiskState.NORMAL,
        RiskState.WITHDRAW,
        RiskState.HEDGE,
        RiskState.RECALIBRATE,
        RiskState.RECALIBRATE,
        RiskState.RECALIBRATE,
        RiskState.REENTER,
        RiskState.NORMAL,
    ]


def test_decision_carries_frame_provenance():
    k = RiskKernel()
    frame = shock_frame(MatchEventType.PENALTY_AWARDED)
    d = k.observe(frame, CALM)
    assert d.sequence == frame.sequence
    assert d.timestamp_ms == frame.timestamp_ms
    fields = d.as_receipt_fields()
    assert fields["state"] == "WITHDRAW"
    assert fields["txlineSequence"] == frame.sequence
    assert fields["triggeredByShock"] is True
    assert 0.0 <= fields["riskScore"] <= 1.0


def test_history_records_every_decision_in_order():
    k = RiskKernel()
    for _ in range(4):
        k.observe(calm_frame(), CALM)
    assert len(k.history) == 4
    assert all(isinstance(d, RiskDecision) for d in k.history)


def test_reset_returns_to_clean_normal():
    k = RiskKernel()
    k.observe(shock_frame(), CALM)
    k.observe(calm_frame(), CALM)
    assert k.state is RiskState.HEDGE
    k.reset()
    assert k.state is RiskState.NORMAL
    assert k.history == []


def test_determinism_same_inputs_same_outputs():
    """Two kernels fed identical frames must produce identical decisions.

    This is the property that makes the Risk Kernel replayable (F8) and its
    transitions independently verifiable against the on-chain receipts (F7).
    """
    def run() -> list[tuple[str, float, str]]:
        k = RiskKernel()
        script = [
            (calm_frame(sequence=1), CALM, False),
            (calm_frame(sequence=2), RiskSignals(consensus_dev=1.0, event_latency=0.4), False),
            (shock_frame(MatchEventType.GOAL, sequence=3), CALM, False),
            (calm_frame(sequence=4), CALM, False),
            (calm_frame(sequence=5), CALM, True),
            (calm_frame(sequence=6), CALM, False),
            (calm_frame(sequence=7), CALM, False),
            (calm_frame(sequence=8), CALM, False),
            (calm_frame(sequence=9), CALM, False),
        ]
        out = []
        for frame, signals, hc in script:
            d = k.observe(frame, signals, hedge_complete=hc)
            out.append((d.state.value, round(d.risk_score, 6), d.reason))
        return out

    assert run() == run()


# --------------------------------------------------------------------------- #
# Constructor guards
# --------------------------------------------------------------------------- #


def test_invalid_thresholds_raise():
    with pytest.raises(ValueError):
        RiskKernel(caution_threshold=0.8, withdraw_threshold=0.5)


def test_invalid_stable_updates_raise():
    with pytest.raises(ValueError):
        RiskKernel(stable_updates_required=0)


def test_negative_reenter_relief_raises():
    with pytest.raises(ValueError):
        RiskKernel(reenter_relief=-0.1)
