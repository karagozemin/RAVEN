"""Receipt store + emitter (F7).

This module ties the two halves of the PROVENANCE layer together:

* :class:`~raven.provenance.receipt.DecisionReceipt` — the deterministic,
  hashable record of *what* RAVEN decided and *why*.
* the anchor backends in :mod:`raven.provenance.anchor` — the mechanism that
  writes the receipt's canonical hash onto Solana devnet.

The :class:`ReceiptEmitter` is the single entry point the rest of the agent
calls. It anchors a receipt (which also computes its canonical hash), appends
the anchored result to an append-only :class:`ReceiptStore`, optionally notifies
a listener, and returns the finalized record. Everything downstream (dashboard,
counterfactual lab, ``verify.ts``) reads from the store.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional

from dataclasses import dataclass

from raven.provenance.anchor import Anchor, AnchorResult, NullAnchor
from raven.provenance.receipt import DecisionReceipt

__all__ = ["AnchoredReceipt", "ReceiptStore", "ReceiptEmitter"]


def _anchor_to_dict(anchor: AnchorResult) -> dict:
    """Serialize an :class:`AnchorResult` (it has no ``to_dict`` of its own)."""
    return {
        "hash": anchor.hash,
        "signature": anchor.signature,
        "anchored": anchor.anchored,
        "backend": anchor.backend,
    }


@dataclass(frozen=True)
class AnchoredReceipt:
    """A decision receipt paired with proof it was anchored on-chain."""

    receipt: DecisionReceipt
    receipt_hash: str
    anchor: AnchorResult

    def to_dict(self) -> dict:
        return {
            "receipt": self.receipt.to_payload(),
            "receipt_hash": self.receipt_hash,
            "anchor": _anchor_to_dict(self.anchor),
        }

    def to_json(self) -> str:
        # sort_keys keeps the on-disk log deterministic so verify.ts can diff it.
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class ReceiptStore:
    """Append-only, thread-safe store of anchored receipts.

    Optionally mirrors every receipt to a JSON-lines file so the log survives a
    restart and can be independently verified after the demo. Order of insertion
    is preserved — this *is* the track record.
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._lock = threading.Lock()
        self._receipts: List[AnchoredReceipt] = []
        self._path: Optional[Path] = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, anchored: AnchoredReceipt) -> None:
        with self._lock:
            self._receipts.append(anchored)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(anchored.to_json() + "\n")

    def __len__(self) -> int:
        with self._lock:
            return len(self._receipts)

    def __iter__(self) -> Iterator[AnchoredReceipt]:
        with self._lock:
            return iter(list(self._receipts))

    def all(self) -> List[AnchoredReceipt]:
        with self._lock:
            return list(self._receipts)

    def latest(self) -> Optional[AnchoredReceipt]:
        with self._lock:
            return self._receipts[-1] if self._receipts else None

    def for_fixture(self, fixture_id: int) -> List[AnchoredReceipt]:
        with self._lock:
            return [a for a in self._receipts if a.receipt.fixture_id == fixture_id]

    def verify(self) -> bool:
        """Recompute every stored hash and confirm it still matches.

        This is the in-process twin of ``verify.ts``: if any receipt was
        tampered with after the fact, the recomputed canonical hash will not
        match the anchored hash and this returns ``False``.
        """
        with self._lock:
            for anchored in self._receipts:
                if anchored.receipt.hash() != anchored.receipt_hash:
                    return False
        return True


class ReceiptEmitter:
    """Anchors, stores, and (optionally) notifies — in one call.

    The agent never touches an :class:`Anchor` directly; it calls
    :meth:`emit` and gets back a fully :class:`AnchoredReceipt`.
    """

    def __init__(
        self,
        anchor: Optional[Anchor] = None,
        store: Optional[ReceiptStore] = None,
        on_emit: Optional[Callable[[AnchoredReceipt], None]] = None,
    ) -> None:
        self._anchor: Anchor = anchor if anchor is not None else NullAnchor()
        self._store: ReceiptStore = store if store is not None else ReceiptStore()
        self._on_emit = on_emit

    @property
    def store(self) -> ReceiptStore:
        return self._store

    def emit(self, receipt: DecisionReceipt) -> AnchoredReceipt:
        # The backend computes the canonical hash as part of anchoring; the
        # returned AnchorResult.hash is authoritative and equals receipt.hash().
        anchor_result = self._anchor.anchor(receipt)
        anchored = AnchoredReceipt(
            receipt=receipt,
            receipt_hash=anchor_result.hash,
            anchor=anchor_result,
        )
        self._store.append(anchored)
        if self._on_emit is not None:
            self._on_emit(anchored)
        return anchored

    def emit_many(self, receipts: Iterable[DecisionReceipt]) -> List[AnchoredReceipt]:
        return [self.emit(r) for r in receipts]
