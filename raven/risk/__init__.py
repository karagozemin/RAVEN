"""RAVEN Risk Kernel (F4).

The Risk Kernel is RAVEN's autonomous nervous system: it converts the stream of
verified frames and derived signals into a single ``RiskState`` and a bounded
``risk_score``, and it drives the quote lifecycle through the state machine

    NORMAL -> CAUTION -> WITHDRAW -> HEDGE -> RECALIBRATE -> REENTER

with **zero manual input**. Every transition is deterministic and logged, so it
can be replayed (F8) and anchored as a decision receipt (F7).
"""

from raven.risk.kernel import (
    RiskState,
    RiskWeights,
    RiskSignals,
    RiskDecision,
    RiskKernel,
)

__all__ = [
    "RiskState",
    "RiskWeights",
    "RiskSignals",
    "RiskDecision",
    "RiskKernel",
]
