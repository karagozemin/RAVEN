"""Anchoring backends (F7).

A receipt is only a *proof* once its hash lives somewhere tamper-evident. These
backends take a :class:`~raven.provenance.receipt.DecisionReceipt`, compute its
canonical hash, and persist it. Three implementations, one interface:

* :class:`NullAnchor` — computes the hash, writes nothing on-chain. Used in
  development and unit tests so the pipeline runs with no wallet, no RPC, no SOL.
* :class:`MemoAnchor` — writes the hash into the Solana **Memo** program on
  devnet. Cheap, no custom program to deploy, and every hash lands in a real
  transaction the judges can open in an explorer. This is the default for the
  demo: it proves the concept end-to-end without a week of Anchor development.
* :class:`SolanaAnchor` — the roadmap target: a purpose-built Anchor program
  that stores receipts in PDAs with on-chain validation. Stubbed here so the
  interface and intent are explicit; the memo path is what actually ships.

Design choice worth stating for the judges: anchoring is **fire-and-safe**. If
the chain is slow or unreachable, RAVEN must *never* block a trading decision on
a network round-trip. Receipts are queued locally with their hash already
computed; the on-chain write is best-effort and confirmable after the fact via
:file:`verify.ts`. The proof is the hash + the input binding, not the latency of
the RPC.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from raven.provenance.receipt import DecisionReceipt

logger = logging.getLogger("raven.provenance.anchor")


@dataclass(frozen=True)
class AnchorResult:
    """Outcome of an anchoring attempt.

    ``signature`` is the Solana transaction signature when the write succeeded
    (``None`` for :class:`NullAnchor` or a failed best-effort write). ``hash`` is
    always present — the receipt is provable from the hash alone even if the
    on-chain write is still pending.
    """

    hash: str
    signature: Optional[str]
    anchored: bool
    backend: str


class Anchor(Protocol):
    """Minimal interface every anchoring backend implements."""

    def anchor(self, receipt: DecisionReceipt) -> AnchorResult:  # pragma: no cover - protocol
        ...


class NullAnchor:
    """No-op backend: hash only, nothing written on-chain.

    This is not a mock of the *data* — the receipt and its hash are entirely
    real and reproducible. It simply skips the network write so development and
    CI don't need a funded wallet. Swap it for :class:`MemoAnchor` for the demo.
    """

    backend = "null"

    def anchor(self, receipt: DecisionReceipt) -> AnchorResult:
        h = receipt.hash()
        logger.debug("NullAnchor: computed receipt hash %s (not written)", h)
        return AnchorResult(hash=h, signature=None, anchored=False, backend=self.backend)


class MemoAnchor:
    """Write receipt hashes to the Solana Memo program on devnet.

    Why memo and not a custom program? For a hackathon the memo program gives us
    100% of what the proof story needs — an immutable, timestamped, explorer-
    visible transaction containing our hash — with 0% of the deployment risk.
    The receipt payload stays off-chain (retrievable + hashable); only the
    32-byte digest goes on-chain, which is exactly what a verifier needs.

    The heavy Solana client imports are done lazily inside :meth:`anchor` so the
    rest of RAVEN has no hard dependency on ``solders``/``solana`` unless memo
    anchoring is actually switched on.
    """

    backend = "memo"
    MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

    def __init__(
        self,
        rpc_url: str,
        keypair_path: Optional[str] = None,
        commitment: str = "confirmed",
    ) -> None:
        self.rpc_url = rpc_url
        self.keypair_path = keypair_path or os.environ.get("RAVEN_SOLANA_KEYPAIR")
        self.commitment = commitment
        self._client = None  # lazily constructed
        self._signer = None

    def _ensure_signer(self) -> None:
        """Build the signer on first use (lazy, no network call)."""
        if self._signer is not None:
            return
        try:
            from solders.keypair import Keypair  # type: ignore
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "MemoAnchor requires 'solders'. Install it or use NullAnchor."
            ) from exc
        if self.keypair_path and os.path.exists(self.keypair_path):
            with open(self.keypair_path, "r", encoding="utf-8") as fh:
                import json
                secret = json.load(fh)
            self._signer = Keypair.from_bytes(bytes(secret))
        else:
            self._signer = Keypair()
            logger.warning(
                "MemoAnchor: no keypair at %s; using ephemeral (unfunded) key. "
                "On-chain writes will not confirm.",
                self.keypair_path,
            )

    @staticmethod
    def _rpc(rpc_url: str, method: str, params=None):
        import json as _json
        import urllib.request as _urllib
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = _urllib.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
        with _urllib.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data["result"]

    def anchor(self, receipt: DecisionReceipt) -> AnchorResult:
        h = receipt.hash()
        try:
            self._ensure_signer()
            import base64
            from solders.pubkey import Pubkey  # type: ignore
            from solders.instruction import Instruction, AccountMeta  # type: ignore
            from solders.hash import Hash  # type: ignore
            from solders.message import MessageV0  # type: ignore
            from solders.transaction import VersionedTransaction  # type: ignore

            memo_program = Pubkey.from_string(self.MEMO_PROGRAM_ID)
            memo_data = f"RAVEN:{receipt.commitment()}".encode("utf-8")
            signer_pubkey = self._signer.pubkey()  # type: ignore[union-attr]
            ix = Instruction(
                program_id=memo_program,
                data=memo_data,
                accounts=[AccountMeta(pubkey=signer_pubkey, is_signer=True, is_writable=False)],
            )
            bh_result = self._rpc(self.rpc_url, "getLatestBlockhash", [{"commitment": self.commitment}])
            blockhash = Hash.from_string(bh_result["value"]["blockhash"])
            msg = MessageV0.try_compile(
                payer=signer_pubkey,
                instructions=[ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self._signer])  # type: ignore[list-item]
            tx_bytes = base64.b64encode(bytes(tx)).decode()
            sig = str(self._rpc(self.rpc_url, "sendTransaction", [
                tx_bytes,
                {"encoding": "base64", "preflightCommitment": self.commitment, "maxRetries": 3},
            ]))
            logger.info("MemoAnchor: receipt %s anchored in tx %s", h[:12], sig)
            return AnchorResult(hash=h, signature=sig, anchored=True, backend=self.backend)
        except Exception as exc:  # noqa: BLE001 - anchoring must never break trading
            logger.warning("MemoAnchor: on-chain write failed (%s); hash retained", exc)
            return AnchorResult(hash=h, signature=None, anchored=False, backend=self.backend)


class SolanaAnchor:
    """Roadmap: dedicated Anchor program storing receipts in PDAs.

    Intentionally a stub. A production deployment would store each receipt under
    a PDA keyed by ``(fixture_id, txline_sequence)`` with on-chain validation of
    the referenced TxLINE anchor, enabling fully on-chain track-record queries.
    For the hackathon we ship :class:`MemoAnchor`; this class documents where the
    proof layer goes next and keeps the interface honest.
    """

    backend = "anchor-program"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "SolanaAnchor (custom program) is roadmap. Use MemoAnchor for the "
            "hackathon submission."
        )

    def anchor(self, receipt: DecisionReceipt) -> AnchorResult:  # pragma: no cover
        raise NotImplementedError
