"""RAVEN agent orchestrator — the loop that ties every layer together.

This is the conductor. It consumes a stream of :class:`VerifiedFrame` objects
from any :class:`~raven.feed.source.FeedSource` (live or replay of real bytes)
and, for each frame, runs the full decision pipeline in order:

    F1 feed  ->  F2 fair value  ->  F3 quotes
                      │
                      ▼
                F4 risk kernel  ->  F6 hedging  ->  F7 receipts

The agent owns the only *mutable* runtime state — the current match state, the
latest consensus odds per market, the inventory book, and a little rolling
history used to derive the risk signals (latency, volatility, incoherence).
Every component it drives is itself deterministic and side-effect free, so the
same ordered frames always produce the same sequence of :class:`TickResult`
objects. That is what makes the whole agent replayable (F8) and its material
decisions independently verifiable (F7).

The agent deliberately does **not** know whether its frames are live or
replayed, nor whether receipts are anchored for real on devnet or stubbed — both
are injected. This keeps the orchestration logic pure and testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from raven.execution.simulated import SimulatedExecution, SimulatedFill
from raven.feed.model import FrameKind, OddsSnapshot, VerifiedFrame
from raven.hedging.engine import HedgeEngine, HedgePlan
from raven.pricing.fair_value import FairValue, FairValueEngine
from raven.pricing.state import MatchState
from raven.provenance.receipt import DecisionReceipt, ReceiptAction
from raven.provenance.store import AnchoredReceipt, ReceiptEmitter
from raven.quoting.engine import QuoteEngine, QuoteSet, SpreadInputs
from raven.quoting.inventory import Inventory
from raven.risk.kernel import RiskDecision, RiskKernel, RiskSignals, RiskState
from raven.risk.adversarial import AdversarialFlowDetector, FillRecord
from raven.risk.dependency_graph import DependencyGraph, MarketState as DependencyMarketState


@dataclass(frozen=True)
class TickResult:
    """Everything the agent decided for a single frame.

    This is the record the Control Room (F10), the counterfactual lab (F8) and
    the demo render from. It carries the driving frame, the resulting risk
    decision, the quotes shown per market, any hedge that was executed, and the
    receipt (if this tick produced a material, anchored decision).
    """

    frame: VerifiedFrame
    state: RiskState
    risk: RiskDecision
    quotes: Dict[str, QuoteSet] = field(default_factory=dict)
    hedge: Optional[HedgePlan] = None
    receipt: Optional[AnchoredReceipt] = None
    fills: List[SimulatedFill] = field(default_factory=list)
    realized_spread_pnl: float = 0.0

    @property
    def is_quoting(self) -> bool:
        return self.state.is_quoting


class _RollingStat:
    """Tiny rolling window for a single scalar signal (volatility / mid drift).

    Kept dependency-free and deterministic: fixed-size FIFO, mean and mean-abs
    change. Used to turn a stream of consensus mids into the normalized
    ``volatility`` signal the Quote Engine and Risk Kernel consume.
    """

    def __init__(self, size: int = 8) -> None:
        self._size = max(2, size)
        self._values: List[float] = []

    def push(self, x: float) -> None:
        self._values.append(float(x))
        if len(self._values) > self._size:
            self._values.pop(0)

    def mean_abs_change(self) -> float:
        if len(self._values) < 2:
            return 0.0
        diffs = [
            abs(self._values[i] - self._values[i - 1])
            for i in range(1, len(self._values))
        ]
        return sum(diffs) / len(diffs)


class RavenAgent:
    """The full RAVEN pipeline as a single, frame-driven object.

    Parameters
    ----------
    fair_value / quote / risk / hedge:
        The four engines. Sensible defaults are constructed when omitted so the
        agent is usable out of the box (and in the smoke test).
    emitter:
        Receipt emitter (F7). Defaults to a store-only emitter with a null
        anchor so the agent runs without a Solana connection; wire a real anchor
        in for devnet.
    latency_budget_ms:
        Provider timestamp lateness (ms), relative to the latest observed
        watermark, that maps to a normalized latency signal of 1.0.
    on_tick:
        Optional callback invoked with every :class:`TickResult` (the dashboard
        and CLI subscribe here).
    """

    def __init__(
        self,
        *,
        fair_value: Optional[FairValueEngine] = None,
        quote: Optional[QuoteEngine] = None,
        risk: Optional[RiskKernel] = None,
        hedge: Optional[HedgeEngine] = None,
        execution: Optional[SimulatedExecution] = None,
        dependency_graph: Optional[DependencyGraph] = None,
        flow_detector: Optional[AdversarialFlowDetector] = None,
        emitter: Optional[ReceiptEmitter] = None,
        latency_budget_ms: float = 2000.0,
        on_tick: Optional[Callable[[TickResult], None]] = None,
    ) -> None:
        self.fair_value = fair_value or FairValueEngine()
        self.quote = quote or QuoteEngine()
        self.risk = risk or RiskKernel()
        self.hedge = hedge or HedgeEngine()
        self.execution = execution or SimulatedExecution()
        self.dependency_graph = dependency_graph or DependencyGraph()
        self.flow_detector = flow_detector or AdversarialFlowDetector()
        self.emitter = emitter or ReceiptEmitter()
        self.latency_budget_ms = max(1.0, latency_budget_ms)
        self._on_tick = on_tick

        # Mutable runtime state.
        self.state = MatchState()
        self.inventory = Inventory()
        self._odds: Dict[str, OddsSnapshot] = {}
        self._fair: Dict[str, FairValue] = {}
        self._vol: Dict[str, _RollingStat] = {}
        self._last_ts_ms: Optional[int] = None
        self._results: List[TickResult] = []
        self._last_quotes: Dict[str, QuoteSet] = {}
        self._last_shock = None
        self._pre_shock_markets: Optional[Dict[str, DependencyMarketState]] = None
        self._coherence_signal = 0.0

    # -- introspection ------------------------------------------------------

    @property
    def results(self) -> List[TickResult]:
        return list(self._results)

    @property
    def odds(self) -> Dict[str, OddsSnapshot]:
        """Latest verified consensus snapshot for every observed market."""
        return dict(self._odds)

    def reset(self) -> None:
        """Clean slate for a fresh replay run (F8 counterfactuals)."""
        self.risk.reset()
        self.state = MatchState()
        self.inventory = Inventory()
        self._odds.clear()
        self._fair.clear()
        self._vol.clear()
        self._last_ts_ms = None
        self._results.clear()
        self.execution = SimulatedExecution()
        self.dependency_graph = DependencyGraph()
        self.flow_detector = AdversarialFlowDetector()
        self._last_quotes.clear()
        self._last_shock = None
        self._pre_shock_markets = None
        self._coherence_signal = 0.0

    # -- main API -----------------------------------------------------------

    def on_frame(self, frame: VerifiedFrame) -> TickResult:
        """Advance the whole pipeline by one verified frame."""
        # Match real consensus moves against the quotes published on the prior
        # tick. Shock frames never fill: the withdrawal reflex wins the race.
        fills = self.execution.process(frame, self.inventory)
        for fill in fills:
            self.flow_detector.record_fill(
                FillRecord(
                    market=fill.market,
                    outcome=fill.outcome,
                    side="buy" if fill.side == "customer_buy" else "sell",
                    size=fill.size,
                    ts_ms=fill.timestamp_ms,
                )
            )

        # 1) Update match state (score/clock/red-cards/finalization).
        self.state = self.state.with_frame(frame)

        if frame.is_shock:
            self._pre_shock_markets = self.dependency_graph.snapshot()
            self.dependency_graph.record_event(frame)
            self._last_shock = frame

        # 2) Refresh the consensus book if this is an odds frame.
        if frame.kind is FrameKind.ODDS and frame.odds is not None:
            self._odds[frame.odds.market] = frame.odds
            self._track_volatility(frame.odds)
            self.dependency_graph.update(frame)
            if self._last_shock is not None:
                elapsed = frame.timestamp_ms - self._last_shock.timestamp_ms
                if 0 <= elapsed <= 15_000:
                    self._coherence_signal = self.dependency_graph.check(
                        self._last_shock.event_type,
                        frame.timestamp_ms,
                        self._pre_shock_markets,
                    ).score
                elif elapsed > 15_000:
                    self._coherence_signal = 0.0

        # 3) Derive normalized risk signals for this tick.
        latency = self._latency_signal(frame)
        signals = self._risk_signals(latency=latency)

        # 4) Risk kernel decides posture. HEDGE completes in one tick here
        #    because the hedge plan is computed synchronously below.
        hedge_complete = self.risk.state is RiskState.HEDGE
        decision = self.risk.observe(
            frame, signals, hedge_complete=hedge_complete
        )

        # 5) Act on the resulting state.
        quotes: Dict[str, QuoteSet] = {}
        hedge_plan: Optional[HedgePlan] = None
        spread_pnl = 0.0
        quotes_cancelled = 0
        inventory_before_action = self.inventory.state_hash()

        if decision.state is RiskState.HEDGE:
            hedge_plan = self._run_hedge()
        elif decision.state.is_quoting:
            quotes, spread_pnl = self._run_quotes(latency=latency)
            self._last_quotes = quotes
            self.execution.publish(quotes)
        elif decision.state is RiskState.WITHDRAW and decision.transitioned:
            quotes_cancelled = self.execution.cancel_all()

        # 6) Anchor a receipt for material decisions (F7).
        receipt = self._maybe_emit_receipt(
            frame,
            decision,
            hedge_plan,
            inventory_before_hash=inventory_before_action,
            inventory_after_hash=self.inventory.state_hash(),
            quotes_cancelled=quotes_cancelled,
            worst_exposure_before=(
                hedge_plan.worst_before.delta if hedge_plan is not None else None
            ),
            worst_exposure_after=(
                hedge_plan.worst_after.delta if hedge_plan is not None else None
            ),
        )

        result = TickResult(
            frame=frame,
            state=decision.state,
            risk=decision,
            quotes=quotes,
            hedge=hedge_plan,
            receipt=receipt,
            fills=fills,
            realized_spread_pnl=round(spread_pnl, 6),
        )
        self._results.append(result)
        if self._on_tick is not None:
            self._on_tick(result)
        return result

    # -- signal derivation --------------------------------------------------

    def _latency_signal(self, frame: VerifiedFrame) -> float:
        """Normalized provider lateness in ``[0, 1]``.

        A gap between two ordered updates is market cadence, not transport
        latency. Treating that gap as staleness made sparse historical updates
        look like a broken feed and could keep RECALIBRATE from ever clearing.
        Instead, retain a provider-time watermark and penalize only frames that
        arrive behind it. This remains deterministic in replay and catches
        stale/out-of-order updates in live merged streams.
        """
        watermark = self._last_ts_ms
        if watermark is None or frame.timestamp_ms is None:
            self._last_ts_ms = frame.timestamp_ms
            return 0.0
        lateness = max(0.0, float(watermark - frame.timestamp_ms))
        self._last_ts_ms = max(watermark, frame.timestamp_ms)
        return min(1.0, lateness / self.latency_budget_ms)

    def _track_volatility(self, odds: OddsSnapshot) -> None:
        stat = self._vol.setdefault(odds.market, _RollingStat())
        raw = odds.implied_raw()
        if raw:
            # Use the favourite's implied prob as a scalar proxy for the book.
            stat.push(max(raw.values()))

    def _volatility_signal(self, market: str) -> float:
        stat = self._vol.get(market)
        if stat is None:
            return 0.0
        # A mean-abs implied-prob change of ~5% per tick maps to full scale.
        return min(1.0, stat.mean_abs_change() / 0.05)

    def _consensus_dev_signal(self) -> float:
        """How far the capped fair value sat from consensus, worst market."""
        worst = 0.0
        for fv in self._fair.values():
            for k, p in fv.probabilities.items():
                mp = fv.market_probs.get(k)
                if mp is not None:
                    worst = max(worst, abs(p - mp))
        # The model-risk cap is 0.08 by default; scale against it.
        return min(1.0, worst / max(self.fair_value.max_deviation, 1e-6))

    def _exposure_signal(self) -> float:
        """Worst-case shock loss relative to a nominal risk budget."""
        exposures = self.hedge.exposures(self.inventory)
        if not exposures:
            return 0.0
        worst = self.hedge.worst(exposures).delta
        loss = max(0.0, -worst)
        # Treat the hedge engine's max single trade as the budget scale.
        return min(1.0, loss / max(self.hedge.max_hedge_size, 1e-6))

    def _risk_signals(self, *, latency: float) -> RiskSignals:
        vol = max(
            (self._volatility_signal(m) for m in self._odds), default=0.0
        )
        toxicity = max(
            (
                signal.toxicity_score
                for signal in self.flow_detector.assess_all(
                    self._last_ts_ms or 0
                ).values()
            ),
            default=0.0,
        )
        return RiskSignals(
            consensus_dev=self._consensus_dev_signal(),
            event_latency=latency,
            cross_market_incoherence=max(vol, self._coherence_signal, toxicity),
            exposure=self._exposure_signal(),
            feed_confidence=0.0 if self._verified(latency) else 0.4,
        ).clipped()

    @staticmethod
    def _verified(latency: float) -> bool:
        return latency < 1.0

    # -- actions ------------------------------------------------------------

    def _run_quotes(self, *, latency: float) -> tuple[Dict[str, QuoteSet], float]:
        """Price and quote every market we have consensus for."""
        quotes: Dict[str, QuoteSet] = {}
        spread_pnl = 0.0
        widen = self.risk.state in {RiskState.CAUTION, RiskState.REENTER}

        for market, odds in self._odds.items():
            fv = self.fair_value.price(odds, self.state)
            self._fair[market] = fv
            if not fv.probabilities:
                continue

            risk_inputs = SpreadInputs(
                event_hazard=1.0 if widen else 0.0,
                latency=latency,
                volatility=self._volatility_signal(market),
                incoherence=max(
                    self._coherence_signal,
                    self.flow_detector.assess(market, self._last_ts_ms or 0).toxicity_score,
                ),
            )
            qs = self.quote.quote(fv, self.inventory, risk=risk_inputs)
            quotes[market] = qs
            # Illustrative captured spread: half-spread on each two-sided quote.
            spread_pnl += sum(
                q.spread * min(q.bid_size, q.ask_size) * 0.5
                for q in qs.quotes.values()
                if not q.withdrawn
            )
        return quotes, spread_pnl

    def _run_hedge(self) -> HedgePlan:
        """Compute and apply the neutralizing cross-market hedge (F6)."""
        self.hedge.hedge_universe = [
            (market, outcome, quote.mid)
            for market, quote_set in self._last_quotes.items()
            for outcome, quote in quote_set.quotes.items()
            if not quote.withdrawn
        ]
        plan = self.hedge.plan(self.inventory)
        for trade in plan.trades:
            self.inventory.apply_fill(
                trade.market,
                trade.outcome,
                trade.signed_quantity(),
                trade.price,
            )
        return plan

    # -- receipts -----------------------------------------------------------

    def _maybe_emit_receipt(
        self,
        frame: VerifiedFrame,
        decision: RiskDecision,
        hedge_plan: Optional[HedgePlan],
        *,
        inventory_before_hash: str,
        inventory_after_hash: str,
        quotes_cancelled: int,
        worst_exposure_before: Optional[float],
        worst_exposure_after: Optional[float],
    ) -> Optional[AnchoredReceipt]:
        """Anchor a receipt for material decisions only (F7).

        Material = a state transition, a withdrawal, or a hedge. Ordinary quote
        refreshes are not individually anchored (keeps on-chain volume sane);
        the state that produced them is captured on transition instead.
        """
        action: Optional[ReceiptAction] = None
        hedge_trades: List[dict] = []

        if hedge_plan is not None and not hedge_plan.is_noop:
            action = ReceiptAction.CANCEL_AND_HEDGE
            hedge_trades = [
                {
                    "market": t.market,
                    "outcome": t.outcome,
                    "side": t.side,
                    "size": t.size,
                    "price": t.price,
                }
                for t in hedge_plan.trades
            ]
        elif decision.state is RiskState.WITHDRAW and decision.transitioned:
            action = ReceiptAction.WITHDRAW
        elif decision.state is RiskState.REENTER and decision.transitioned:
            action = ReceiptAction.REENTER

        if action is None:
            return None

        receipt = DecisionReceipt(
            action=action,
            reason=decision.reason,
            fixture_id=int(frame.fixture_id or 0),
            txline_sequence=int(frame.provider_sequence or frame.sequence),
            market_state_hash=frame.payload_hash,
            risk_score=decision.risk_score,
            previous_state=decision.prior_state.value,
            new_state=decision.state.value,
            inventory_before_hash=inventory_before_hash,
            inventory_after_hash=inventory_after_hash,
            quotes_cancelled=quotes_cancelled,
            hedge_trades=hedge_trades,
            worst_exposure_before=worst_exposure_before,
            worst_exposure_after=worst_exposure_after,
            execution_timestamp=int(frame.timestamp_ms or _now_ms()),
        )
        return self.emitter.emit(receipt)


def _now_ms() -> int:
    return int(time.time() * 1000)
