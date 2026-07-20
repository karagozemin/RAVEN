from __future__ import annotations

from raven.agent import RavenAgent
from raven.counterfactual import run_counterfactual
from raven.provenance.anchor import ArchiveAnchor
from raven.provenance.store import ReceiptEmitter
from raven.web.driver import iter_verified_frames


def _run(agent: RavenAgent | None = None) -> RavenAgent:
    raven = agent or RavenAgent()
    for frame in iter_verified_frames():
        raven.on_frame(frame)
    return raven


def test_packaged_replay_is_real_multimarket_and_monotonic() -> None:
    frames = list(iter_verified_frames())
    assert len(frames) == 1976
    assert [frame.sequence for frame in frames] == list(range(1, 1977))
    assert {frame.odds.market for frame in frames if frame.odds} == {
        "match_winner",
        "asian_handicap@-0.5",
        "total_goals@2.5",
    }
    assert all(frame.fixture_id == 18222446 for frame in frames)
    proven = [frame for frame in frames if frame.verified]
    assert [(frame.provider_sequence, frame.event_type.value) for frame in proven] == [
        (118, "GOAL")
    ]
    assert proven[0].score is not None
    assert proven[0].score.as_tuple() == (1, 0)


def test_full_agent_executes_fills_withdrawals_and_improving_hedges() -> None:
    raven = _run()
    assert sum(len(result.fills) for result in raven.results) > 0
    assert raven.inventory.total_notional() > 0
    assert any(
        result.receipt
        and result.receipt.receipt.action.value == "WITHDRAW"
        and result.receipt.receipt.quotes_cancelled > 0
        for result in raven.results
    )
    hedges = [
        result.hedge
        for result in raven.results
        if result.hedge is not None and not result.hedge.is_noop
    ]
    assert hedges
    assert all(
        abs(plan.worst_after.delta) < abs(plan.worst_before.delta)
        for plan in hedges
    )


def test_replay_decisions_are_deterministic() -> None:
    first = _run()
    second = _run()

    def decisions(agent: RavenAgent) -> list[tuple]:
        return [
            (
                result.frame.sequence,
                result.state.value,
                tuple(
                    (trade.market, trade.outcome, trade.side, trade.size)
                    for trade in (result.hedge.trades if result.hedge else [])
                ),
                result.receipt.receipt_hash if result.receipt else None,
            )
            for result in agent.results
        ]

    assert decisions(first) == decisions(second)


def test_archived_receipts_match_current_replay() -> None:
    anchor = ArchiveAnchor("receipts/anchored_demo.json")
    raven = _run(RavenAgent(emitter=ReceiptEmitter(anchor=anchor)))
    anchored = [
        result.receipt
        for result in raven.results
        if result.receipt is not None and result.receipt.anchor.anchored
    ]
    assert [receipt.receipt.action.value for receipt in anchored] == [
        "WITHDRAW",
        "CANCEL_AND_HEDGE",
        "WITHDRAW",
        "REENTER",
    ]
    assert anchored[2].receipt.txline_sequence == 118
    assert all(receipt.anchor.signature for receipt in anchored)


def test_counterfactual_uses_same_frames_and_reduces_peak_risk() -> None:
    result = run_counterfactual(print_result=False)
    assert result.baseline.frames == result.raven.frames == 1976
    assert result.baseline.peak_worst_case_loss > result.raven.peak_worst_case_loss
    expected = round(
        (
            result.baseline.peak_worst_case_loss
            - result.raven.peak_worst_case_loss
        )
        / result.baseline.peak_worst_case_loss
        * 100.0,
        2,
    )
    assert result.peak_risk_reduction == expected
    assert result.peak_risk_reduction > 80.0
    assert result.baseline.manual_interventions == 3
    assert result.raven.manual_interventions == 0
