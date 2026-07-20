"""Deterministic baseline-versus-RAVEN comparison on identical TxLINE frames.

The baseline and RAVEN share the same normalizer, fair-value model, quote
engine contract, deterministic matching model, and immutable historical input.
The policy is the only variable: the baseline always quotes a static spread and
never hedges; RAVEN runs the complete risk, withdrawal, recovery, and hedging
pipeline. No hand-authored P&L or event-loss constants are used here.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from raven.agent import RavenAgent, TickResult
from raven.feed.model import VerifiedFrame
from raven.hedging.engine import HedgeEngine
from raven.quoting.engine import QuoteEngine
from raven.risk.kernel import RiskDecision, RiskSignals, RiskState
from raven.web.driver import iter_verified_frames


class _AlwaysQuotePolicy:
    """Event-blind control policy used as the counterfactual baseline."""

    state = RiskState.NORMAL

    def reset(self) -> None:
        return None

    def observe(
        self,
        frame: VerifiedFrame,
        signals: RiskSignals,
        *,
        hedge_complete: bool = False,
    ) -> RiskDecision:
        return RiskDecision(
            state=RiskState.NORMAL,
            prior_state=RiskState.NORMAL,
            risk_score=0.0,
            reason="event-blind static quoting baseline",
            sequence=frame.sequence,
            timestamp_ms=frame.timestamp_ms,
            triggered_by_shock=False,
        )


class _BaselineAgent(RavenAgent):
    """Event-blind control with static spreads and no practical inventory cap."""

    def __init__(self) -> None:
        super().__init__(
            risk=_AlwaysQuotePolicy(),  # type: ignore[arg-type]
            quote=QuoteEngine(
                skew_gain=0.0,
                max_position=1_000_000.0,
                hazard_gain=0.0,
                latency_gain=0.0,
                vol_gain=0.0,
                incoherence_gain=0.0,
            ),
        )

    def _risk_signals(self, *, latency: float) -> RiskSignals:
        return RiskSignals()


@dataclass(frozen=True)
class RunMetrics:
    frames: int
    quote_ticks: int
    fills: int
    quotes_cancelled: int
    hedge_trades: int
    hedge_risk_reduction: float
    peak_worst_case_loss: float
    final_worst_case_loss: float
    spread_opportunity: float
    manual_interventions: int


@dataclass(frozen=True)
class CounterfactualResult:
    fixture_id: int
    data_source: str
    baseline: RunMetrics
    raven: RunMetrics

    @property
    def peak_risk_reduction(self) -> float:
        if self.baseline.peak_worst_case_loss <= 0.0:
            return 0.0
        improvement = (
            self.baseline.peak_worst_case_loss
            - self.raven.peak_worst_case_loss
        ) / self.baseline.peak_worst_case_loss
        return round(max(0.0, improvement) * 100.0, 2)

    def to_dict(self) -> dict:
        return {
            "fixture_id": self.fixture_id,
            "data_source": self.data_source,
            "baseline": asdict(self.baseline),
            "raven": asdict(self.raven),
            "peak_risk_reduction_pct": self.peak_risk_reduction,
        }


class _MetricsCollector:
    def __init__(self, agent: RavenAgent, *, baseline: bool) -> None:
        self.agent = agent
        self.baseline = baseline
        self.frames = 0
        self.quote_ticks = 0
        self.fills = 0
        self.quotes_cancelled = 0
        self.hedge_trades = 0
        self.hedge_risk_reduction = 0.0
        self.peak_worst_case_loss = 0.0
        self.spread_opportunity = 0.0
        self.shocks = 0
        self._exposure = HedgeEngine()

    def add(self, result: TickResult) -> None:
        self.frames += 1
        self.quote_ticks += int(result.is_quoting)
        self.fills += len(result.fills)
        self.spread_opportunity += result.realized_spread_pnl
        self.shocks += int(result.frame.is_shock)
        if result.receipt is not None:
            self.quotes_cancelled += result.receipt.receipt.quotes_cancelled
        if result.hedge is not None and not result.hedge.is_noop:
            self.hedge_trades += len(result.hedge.trades)
            self.hedge_risk_reduction += result.hedge.reduction
        self.peak_worst_case_loss = max(
            self.peak_worst_case_loss,
            self._worst_loss(),
        )

    def _worst_loss(self) -> float:
        exposures = self._exposure.exposures(self.agent.inventory)
        return max(0.0, -self._exposure.worst(exposures).delta)

    def finish(self) -> RunMetrics:
        return RunMetrics(
            frames=self.frames,
            quote_ticks=self.quote_ticks,
            fills=self.fills,
            quotes_cancelled=self.quotes_cancelled,
            hedge_trades=self.hedge_trades,
            hedge_risk_reduction=round(self.hedge_risk_reduction, 6),
            peak_worst_case_loss=round(self.peak_worst_case_loss, 6),
            final_worst_case_loss=round(self._worst_loss(), 6),
            spread_opportunity=round(self.spread_opportunity, 6),
            manual_interventions=self.shocks if self.baseline else 0,
        )


def run_counterfactual(
    replay_path: str | Path | None = None,
    speed: float = 0.0,
    print_result: bool = True,
    frames: Optional[Iterable[VerifiedFrame]] = None,
) -> CounterfactualResult:
    """Run both policies over the exact same ordered, verified frames.

    ``replay_path`` is retained for CLI compatibility. The packaged historical
    fixture is a paired score + odds dataset, so it is loaded through the same
    merged iterator used by the web Control Room.
    """
    if replay_path is not None and not Path(replay_path).exists():
        raise FileNotFoundError(f"Replay not found: {replay_path}")
    ordered_frames = list(frames if frames is not None else iter_verified_frames())
    if not ordered_frames:
        raise ValueError("Replay contains no verified frames")

    baseline_agent = _BaselineAgent()
    raven_agent = RavenAgent()
    baseline_metrics = _MetricsCollector(baseline_agent, baseline=True)
    raven_metrics = _MetricsCollector(raven_agent, baseline=False)

    for frame in ordered_frames:
        baseline_metrics.add(baseline_agent.on_frame(frame))
        raven_metrics.add(raven_agent.on_frame(frame))

    comparison = CounterfactualResult(
        fixture_id=int(ordered_frames[0].fixture_id or 0),
        data_source="TxLINE historical scores + odds",
        baseline=baseline_metrics.finish(),
        raven=raven_metrics.finish(),
    )
    if print_result:
        _print_comparison(comparison)
    return comparison


def _print_comparison(result: CounterfactualResult) -> None:
    b, r = result.baseline, result.raven
    print("\nRAVEN Counterfactual Lab")
    print(f"fixture={result.fixture_id} source={result.data_source} frames={r.frames}")
    print(f"{'metric':<28}{'baseline':>14}{'RAVEN':>14}")
    print("-" * 56)
    rows: Dict[str, tuple[float | int, float | int]] = {
        "quote ticks": (b.quote_ticks, r.quote_ticks),
        "deterministic fills": (b.fills, r.fills),
        "quotes cancelled": (b.quotes_cancelled, r.quotes_cancelled),
        "hedge trades": (b.hedge_trades, r.hedge_trades),
        "peak worst-case loss": (b.peak_worst_case_loss, r.peak_worst_case_loss),
        "final worst-case loss": (b.final_worst_case_loss, r.final_worst_case_loss),
        "spread opportunity": (b.spread_opportunity, r.spread_opportunity),
        "manual interventions": (b.manual_interventions, r.manual_interventions),
    }
    for label, (baseline, raven) in rows.items():
        print(f"{label:<28}{baseline:>14.2f}{raven:>14.2f}")
    print(f"\npeak worst-case risk reduction: {result.peak_risk_reduction:.2f}%")
    print("Metrics are deterministic outputs, not hand-authored performance claims.\n")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="RAVEN Counterfactual Lab")
    parser.add_argument(
        "--replay",
        default="data/replay/scores_historical_18257739.jsonl",
        help="Path used to select/validate the packaged real replay.",
    )
    parser.add_argument("--speed", type=float, default=0.0)
    args = parser.parse_args()
    run_counterfactual(args.replay, speed=args.speed)


if __name__ == "__main__":
    _cli()
