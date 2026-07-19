"""Risk Kernel / State Machine for RAVEN (F4).

This is the layer that decides *whether RAVEN should be in the market at all*,
and if so, how nervous it should be. It sits between the signal producers
(Fair-Value, feed latency, Market Dependency Graph, Adversarial Flow Detector)
and the Quote Engine, and it owns the single source of truth for RAVEN's
posture: the :class:`RiskState`.

The lifecycle (FR4.1) is a strict, deterministic progression::

    NORMAL ─▶ CAUTION ─▶ WITHDRAW ─▶ HEDGE ─▶ RECALIBRATE ─▶ REENTER ─▶ NORMAL
       ▲                                                                  │
       └──────────────────────────────────────────────────────────────┘

* **NORMAL** — calm; quote at base spread.
* **CAUTION** — risk is elevated (rising hazard/latency/incoherence) but no
  verified shock. Widen, don't withdraw.
* **WITHDRAW** — a verified shock frame arrived (FR4.2): cancel all open quotes
  immediately. This is the millisecond reflex the demo hinges on.
* **HEDGE** — quotes are down; hand off to the Self-Hedging Engine (F6) to
  neutralize the shock exposure.
* **RECALIBRATE** — wait for the consensus to settle: N consecutive stable,
  verified updates with no new shock.
* **REENTER** — stability achieved; step back in with tightened-but-recovering
  spreads, then fall back to NORMAL.

The risk score (FR4.3) is a bounded weighted blend of five normalized signals::

    risk = 0.30·consensusDev + 0.25·eventLatency + 0.20·crossMarketIncoherence
         + 0.15·exposure     + 0.10·feedConfidence

Everything here is deterministic and side-effect free: the same ordered inputs
always produce the same sequence of :class:`RiskDecision` objects, which is what
makes the kernel replayable (F8) and its transitions anchorable (F7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from raven.feed.model import VerifiedFrame


class RiskState(str, Enum):
    """RAVEN's market posture. See module docstring for the lifecycle."""

    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    WITHDRAW = "WITHDRAW"
    HEDGE = "HEDGE"
    RECALIBRATE = "RECALIBRATE"
    REENTER = "REENTER"

    @property
    def is_quoting(self) -> bool:
        """States in which RAVEN shows two-sided quotes."""
        return self in {
            RiskState.NORMAL,
            RiskState.CAUTION,
            RiskState.REENTER,
        }

    @property
    def is_withdrawn(self) -> bool:
        """States in which RAVEN is standing aside (no live size)."""
        return self in {
            RiskState.WITHDRAW,
            RiskState.HEDGE,
            RiskState.RECALIBRATE,
        }


def _clip01(x: float) -> float:
    """Clamp to the unit interval; all risk signals live in [0, 1]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass(frozen=True)
class RiskWeights:
    """Weights for the FR4.3 risk-score blend.

    The defaults are the exact coefficients from the PRD. They sum to 1.0 so the
    resulting ``risk_score`` is itself bounded in ``[0, 1]`` whenever every input
    signal is normalized to ``[0, 1]`` — a property the kernel relies on for its
    threshold logic and which :meth:`validate` enforces.
    """

    consensus_dev: float = 0.30
    event_latency: float = 0.25
    cross_market_incoherence: float = 0.20
    exposure: float = 0.15
    feed_confidence: float = 0.10

    def validate(self) -> "RiskWeights":
        total = (
            self.consensus_dev
            + self.event_latency
            + self.cross_market_incoherence
            + self.exposure
            + self.feed_confidence
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"risk weights must sum to 1.0, got {total}")
        return self


@dataclass(frozen=True)
class RiskSignals:
    """Normalized ``[0, 1]`` risk inputs for a single tick (FR4.3).

    Each field is produced by a different part of the system and clipped to the
    unit interval here so the weighted blend stays bounded:

    * ``consensus_dev`` — how far RAVEN's fair value has drifted from the TxLINE
      consensus (0 = on top of consensus, 1 = at the model-risk cap).
    * ``event_latency`` — staleness of the verified feed relative to the tolerated
      budget (0 = fresh, 1 = at/over budget).
    * ``cross_market_incoherence`` — Market Dependency Graph signal (F5): linked
      markets have not repriced consistently with the leader.
    * ``exposure`` — current portfolio exposure relative to limits (0 = flat,
      1 = at max risk).
    * ``feed_confidence`` — inverse confidence in the feed: 0 = fully verified /
      trusted, 1 = unverified / degraded. (Named to match the PRD term; higher
      means *worse*, so it adds risk like the others.)
    """

    consensus_dev: float = 0.0
    event_latency: float = 0.0
    cross_market_incoherence: float = 0.0
    exposure: float = 0.0
    feed_confidence: float = 0.0

    def clipped(self) -> "RiskSignals":
        return RiskSignals(
            consensus_dev=_clip01(self.consensus_dev),
            event_latency=_clip01(self.event_latency),
            cross_market_incoherence=_clip01(self.cross_market_incoherence),
            exposure=_clip01(self.exposure),
            feed_confidence=_clip01(self.feed_confidence),
        )

    def score(self, weights: RiskWeights) -> float:
        """Bounded weighted blend — the FR4.3 risk score in ``[0, 1]``."""
        s = self.clipped()
        return (
            weights.consensus_dev * s.consensus_dev
            + weights.event_latency * s.event_latency
            + weights.cross_market_incoherence * s.cross_market_incoherence
            + weights.exposure * s.exposure
            + weights.feed_confidence * s.feed_confidence
        )


@dataclass(frozen=True)
class RiskDecision:
    """The kernel's output for one tick.

    Carries the resulting state, the bounded ``risk_score`` that produced it, a
    human-readable ``reason`` (surfaced by the LLM Decision Inspector, F11, and
    written into the on-chain receipt, F7), and the provenance of the frame that
    drove the decision so it can be independently verified.
    """

    state: RiskState
    prior_state: RiskState
    risk_score: float
    reason: str
    sequence: Optional[int] = None
    timestamp_ms: Optional[int] = None
    triggered_by_shock: bool = False

    @property
    def transitioned(self) -> bool:
        return self.state is not self.prior_state

    @property
    def should_withdraw(self) -> bool:
        """True when the Quote Engine must stand aside this tick."""
        return self.state.is_withdrawn

    def as_receipt_fields(self) -> dict:
        """Compact dict for embedding in a Solana decision receipt (F7)."""
        return {
            "state": self.state.value,
            "priorState": self.prior_state.value,
            "riskScore": round(self.risk_score, 6),
            "reason": self.reason,
            "txlineSequence": self.sequence,
            "executionTimestamp": self.timestamp_ms,
            "triggeredByShock": self.triggered_by_shock,
        }


class RiskKernel:
    """Deterministic state machine driving RAVEN's market posture (F4).

    Parameters
    ----------
    weights:
        Coefficients for the FR4.3 risk-score blend. Must sum to 1.0.
    caution_threshold / withdraw_threshold:
        Risk-score bands. ``risk >= withdraw_threshold`` forces WITHDRAW even
        without a discrete shock frame; ``risk >= caution_threshold`` moves
        NORMAL into CAUTION. A verified *shock frame* always forces WITHDRAW
        regardless of score (FR4.2) — the reflex must not depend on a threshold.
    stable_updates_required:
        Number of consecutive stable, verified, shock-free updates required in
        RECALIBRATE before RAVEN is allowed to REENTER (the PRD's "3 consecutive
        stable updates").
    reenter_relief:
        How much the risk score must fall *below* ``caution_threshold`` before
        REENTER hands back to NORMAL, providing hysteresis so RAVEN doesn't
        flap on the boundary.

    Notes
    -----
    The kernel keeps only the minimal state needed to be deterministic: the
    current posture and a counter of consecutive stable ticks. It never reaches
    into wall-clock time or randomness.
    """

    def __init__(
        self,
        *,
        weights: Optional[RiskWeights] = None,
        caution_threshold: float = 0.35,
        withdraw_threshold: float = 0.65,
        stable_updates_required: int = 3,
        reenter_relief: float = 0.10,
    ) -> None:
        if not 0.0 <= caution_threshold <= withdraw_threshold <= 1.0:
            raise ValueError(
                "require 0 <= caution_threshold <= withdraw_threshold <= 1"
            )
        if stable_updates_required < 1:
            raise ValueError("stable_updates_required must be >= 1")
        if reenter_relief < 0.0:
            raise ValueError("reenter_relief must be >= 0")

        self.weights = (weights or RiskWeights()).validate()
        self.caution_threshold = caution_threshold
        self.withdraw_threshold = withdraw_threshold
        self.stable_updates_required = stable_updates_required
        self.reenter_relief = reenter_relief

        self._state: RiskState = RiskState.NORMAL
        self._stable_count: int = 0
        self._history: List[RiskDecision] = []

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def history(self) -> List[RiskDecision]:
        """Immutable-ish view of every decision, in order (for F8 replay/F7)."""
        return list(self._history)

    # -- main API -----------------------------------------------------------

    def observe(
        self,
        frame: VerifiedFrame,
        signals: RiskSignals,
        *,
        hedge_complete: bool = False,
    ) -> RiskDecision:
        """Advance the state machine by one verified frame.

        Parameters
        ----------
        frame:
            The verified frame for this tick. A ``frame.is_shock`` frame is the
            hard trigger for an immediate WITHDRAW (FR4.2).
        signals:
            Normalized risk signals for the FR4.3 score.
        hedge_complete:
            Set by the main loop once the Self-Hedging Engine (F6) reports the
            shock exposure has been neutralized. This is what lets HEDGE advance
            to RECALIBRATE — the kernel does not execute hedges itself, it only
            sequences them.

        Returns
        -------
        RiskDecision
            The (possibly unchanged) state plus score, reason and provenance.
        """
        score = signals.score(self.weights)
        prior = self._state
        is_shock = frame.is_shock

        next_state, reason = self._next_state(
            score=score,
            is_shock=is_shock,
            frame=frame,
            hedge_complete=hedge_complete,
        )

        # Maintain the consecutive-stable counter used by RECALIBRATE.
        self._update_stability(next_state, score, is_shock)

        self._state = next_state
        decision = RiskDecision(
            state=next_state,
            prior_state=prior,
            risk_score=score,
            reason=reason,
            sequence=frame.sequence,
            timestamp_ms=frame.timestamp_ms,
            triggered_by_shock=is_shock,
        )
        self._history.append(decision)
        return decision

    def reset(self) -> None:
        """Return to a clean NORMAL posture (used between replay runs, F8)."""
        self._state = RiskState.NORMAL
        self._stable_count = 0
        self._history.clear()

    # -- transition logic ---------------------------------------------------

    def _next_state(
        self,
        *,
        score: float,
        is_shock: bool,
        frame: VerifiedFrame,
        hedge_complete: bool,
    ) -> tuple[RiskState, str]:
        """Pure transition function: (current, inputs) -> (next, reason).

        A verified shock always short-circuits to WITHDRAW from any quoting or
        cautionary state, because the reflex must never wait on a score band.
        """
        state = self._state

        # Hard reflex (FR4.2): a verified shock forces WITHDRAW from any posture
        # that is currently exposed to the market.
        if is_shock and state in {
            RiskState.NORMAL,
            RiskState.CAUTION,
            RiskState.REENTER,
            RiskState.RECALIBRATE,
        }:
            ev = frame.event_type.value
            return (
                RiskState.WITHDRAW,
                f"verified {ev} (seq #{frame.sequence}) — cancel all quotes",
            )

        if state is RiskState.NORMAL:
            if score >= self.withdraw_threshold:
                return (
                    RiskState.WITHDRAW,
                    f"risk score {score:.3f} >= withdraw "
                    f"{self.withdraw_threshold:.2f}",
                )
            if score >= self.caution_threshold:
                return (
                    RiskState.CAUTION,
                    f"risk score {score:.3f} >= caution "
                    f"{self.caution_threshold:.2f}; widening",
                )
            return (RiskState.NORMAL, "calm; quoting at base spread")

        if state is RiskState.CAUTION:
            if score >= self.withdraw_threshold:
                return (
                    RiskState.WITHDRAW,
                    f"risk score {score:.3f} >= withdraw "
                    f"{self.withdraw_threshold:.2f}",
                )
            if score < self.caution_threshold:
                return (RiskState.NORMAL, "risk subsided; back to base spread")
            return (RiskState.CAUTION, f"elevated risk {score:.3f}; widened")

        if state is RiskState.WITHDRAW:
            # Once withdrawn, immediately move to neutralize exposure.
            return (
                RiskState.HEDGE,
                "quotes cancelled; neutralizing shock exposure",
            )

        if state is RiskState.HEDGE:
            if hedge_complete:
                return (
                    RiskState.RECALIBRATE,
                    "exposure neutralized; waiting for consensus to settle",
                )
            return (RiskState.HEDGE, "hedging shock exposure")

        if state is RiskState.RECALIBRATE:
            if self._stable_count + 1 >= self.stable_updates_required:
                return (
                    RiskState.REENTER,
                    f"{self.stable_updates_required} stable updates; "
                    "re-entering",
                )
            return (
                RiskState.RECALIBRATE,
                f"awaiting stable consensus "
                f"({self._stable_count + 1}/{self.stable_updates_required})",
            )

        if state is RiskState.REENTER:
            if score >= self.withdraw_threshold:
                return (
                    RiskState.WITHDRAW,
                    f"risk spiked to {score:.3f} during re-entry",
                )
            if score <= self.caution_threshold - self.reenter_relief:
                return (RiskState.NORMAL, "re-entry complete; back to normal")
            return (RiskState.REENTER, "re-entering with recovering spreads")

        # Defensive default; unreachable for the closed enum above.
        return (state, "no change")

    def _update_stability(
        self, next_state: RiskState, score: float, is_shock: bool
    ) -> None:
        """Track consecutive stable, shock-free ticks for RECALIBRATE.

        A tick counts as *stable* only while recalibrating, when there is no
        shock and the score has fallen back below the caution band. Any shock or
        elevated score resets the counter, forcing RAVEN to wait afresh.
        """
        if next_state is RiskState.RECALIBRATE:
            if not is_shock and score < self.caution_threshold:
                self._stable_count += 1
            else:
                self._stable_count = 0
        else:
            # Leaving (or not in) RECALIBRATE clears the counter.
            self._stable_count = 0
