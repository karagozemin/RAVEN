"""Deterministic decision receipts (F7).

A :class:`DecisionReceipt` is the canonical, tamper-evident record of a single
RAVEN decision. It captures *what* RAVEN did, *why*, and — critically — the exact
TxLINE input that triggered it (by sequence and payload hash) plus the policy
version that made the call. Two properties matter:

* **Deterministic** — the same decision on the same inputs always serializes to
  the same bytes, so the hash is reproducible in :file:`verify.ts`.
* **Self-contained** — a receipt carries everything a third party needs to
  re-derive the hash without trusting RAVEN's database.

The hash we anchor on-chain is :func:`canonical_hash`: SHA-256 over a
canonically-encoded JSON object (sorted keys, no insignificant whitespace,
fixed float formatting). Get the encoding wrong and the TypeScript verifier
would disagree with the Python producer — so the encoding is defined here once
and mirrored exactly in :file:`verify.ts`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ReceiptAction(str, Enum):
    """The kinds of decisions worth anchoring.

    Not every tick produces a receipt — only *material* decisions do, to keep
    on-chain volume sane. Quote refreshes are batched; withdrawals, hedges and
    state transitions are always recorded because they are the ones a trading
    desk or auditor would ever dispute.
    """

    QUOTE = "QUOTE"
    WIDEN = "WIDEN"
    WITHDRAW = "WITHDRAW"
    CANCEL_AND_HEDGE = "CANCEL_AND_HEDGE"
    HEDGE = "HEDGE"
    REENTER = "REENTER"
    STATE_TRANSITION = "STATE_TRANSITION"


# Bump when the receipt schema or hashing changes. verify.ts pins the same
# string, so a mismatch is a loud, obvious failure rather than a silent one.
POLICY_VERSION = "raven-v1.1.0"


def _fmt_float(x: float) -> float | int:
    """Round floats to a fixed precision for hash stability.

    Floating point is the classic cross-language hashing footgun: Python and
    JS can print the same number differently. We round to 6 dp everywhere so the
    canonical bytes are identical on both sides.
    """
    rounded = round(float(x), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _canonicalize(value: Any) -> Any:
    """Recursively coerce a value into hash-stable, JSON-safe primitives."""
    if isinstance(value, float):
        return _fmt_float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def canonical_json(payload: Dict[str, Any]) -> str:
    """Canonical JSON encoding used for hashing.

    Sorted keys + compact separators + normalized values. This exact string is
    what both the anchor and :file:`verify.ts` hash, so it must be byte-for-byte
    reproducible.
    """
    return json.dumps(
        _canonicalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_hash(payload: Dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical JSON encoding of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionReceipt:
    """One anchored decision.

    Fields mirror the PRD receipt schema (§8.2). ``txline_sequence`` and
    ``market_state_hash`` bind the decision to a *specific* verified feed
    update, so the receipt can never be re-attributed to different data after
    the fact.
    """

    action: ReceiptAction
    reason: str
    fixture_id: int
    txline_sequence: int
    market_state_hash: str
    risk_score: float
    previous_state: str
    new_state: str
    inventory_before_hash: str
    inventory_after_hash: str
    quotes_cancelled: int = 0
    hedge_trades: List[Dict[str, Any]] = field(default_factory=list)
    worst_exposure_before: Optional[float] = None
    worst_exposure_after: Optional[float] = None
    execution_timestamp: int = 0
    policy_version: str = POLICY_VERSION

    def _decision_payload(self) -> Dict[str, Any]:
        """Canonical decision fields covered by the on-chain commitment."""
        payload: Dict[str, Any] = {
            "action": self.action.value,
            "reason": self.reason,
            "fixtureId": self.fixture_id,
            "txlineSequence": self.txline_sequence,
            "marketStateHash": self.market_state_hash,
            "riskScore": _fmt_float(self.risk_score),
            "previousState": self.previous_state,
            "newState": self.new_state,
            "inventoryBefore": self.inventory_before_hash,
            "inventoryAfter": self.inventory_after_hash,
            "quotesCancelled": self.quotes_cancelled,
            "hedgeTransactions": self.hedge_trades,
            "executionTimestamp": self.execution_timestamp,
            "policyHash": self.policy_version,
        }
        if self.worst_exposure_before is not None:
            payload["worstExposureBefore"] = _fmt_float(
                self.worst_exposure_before
            )
        if self.worst_exposure_after is not None:
            payload["worstExposureAfter"] = _fmt_float(
                self.worst_exposure_after
            )
        return payload

    def to_payload(self) -> Dict[str, Any]:
        """The self-contained dict stored off-chain and independently verified.

        We build it explicitly (rather than dumping ``asdict``) so the on-chain
        field order/naming is a deliberate contract with :file:`verify.ts`, not
        an accident of dataclass layout.
        """
        payload = self._decision_payload()
        payload["commitmentHash"] = self.commitment()
        return payload

    def commitment(self) -> str:
        """Hash every decision field; this exact digest is written to Solana."""
        return canonical_hash(self._decision_payload())

    def hash(self) -> str:
        """Deterministic SHA-256 of this receipt's canonical payload."""
        return canonical_hash(self.to_payload())

    def to_json(self) -> str:
        """Human/verifier-facing JSON (canonical, so hash-reproducible)."""
        return canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "DecisionReceipt":
        """Rebuild a receipt from its anchored payload (for tests/verify)."""
        return cls(
            action=ReceiptAction(payload["action"]),
            reason=payload["reason"],
            fixture_id=int(payload["fixtureId"]),
            txline_sequence=int(payload["txlineSequence"]),
            market_state_hash=payload["marketStateHash"],
            risk_score=float(payload["riskScore"]),
            previous_state=payload["previousState"],
            new_state=payload["newState"],
            inventory_before_hash=payload["inventoryBefore"],
            inventory_after_hash=payload["inventoryAfter"],
            quotes_cancelled=int(payload.get("quotesCancelled", 0)),
            hedge_trades=list(payload.get("hedgeTransactions", [])),
            worst_exposure_before=(
                float(payload["worstExposureBefore"])
                if payload.get("worstExposureBefore") is not None
                else None
            ),
            worst_exposure_after=(
                float(payload["worstExposureAfter"])
                if payload.get("worstExposureAfter") is not None
                else None
            ),
            execution_timestamp=int(payload.get("executionTimestamp", 0)),
            policy_version=payload.get("policyHash", POLICY_VERSION),
        )
