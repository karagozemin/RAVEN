from raven.agent import RavenAgent
from raven.feed.model import VerifiedFrame


def _frame(sequence: int, timestamp_ms: int) -> VerifiedFrame:
    return VerifiedFrame(
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload_hash=f"hash-{sequence}",
    )


def test_sparse_ordered_updates_are_not_transport_latency() -> None:
    agent = RavenAgent(latency_budget_ms=2_000)
    assert agent._latency_signal(_frame(1, 1_000)) == 0.0
    assert agent._latency_signal(_frame(2, 11_000)) == 0.0


def test_frame_behind_provider_watermark_is_late() -> None:
    agent = RavenAgent(latency_budget_ms=2_000)
    agent._latency_signal(_frame(1, 10_000))
    assert agent._latency_signal(_frame(2, 9_000)) == 0.5
    assert agent._latency_signal(_frame(3, 7_000)) == 1.0

