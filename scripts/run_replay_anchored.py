#!/usr/bin/env python3
"""Full replay of real TxLINE data with live Solana devnet anchoring.

Reads data/replay/scores_historical_18222446.jsonl (raw TxLINE records),
normalizes each through the RAVEN pipeline, and anchors every material
decision to Solana devnet via the Memo program.  Saves each receipt as
receipts/receipt_<seq>.json so verify.ts can verify them 7/7.

Requires: solana, solders installed (.venv/bin/pip install solana solders)
          _keys/raven-wallet.json funded on devnet (~10 SOL present)
"""
from __future__ import annotations

import base64, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests as _req

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from raven.agent import RavenAgent
from raven.feed.normalize import normalize
from raven.provenance.anchor import AnchorResult
from raven.provenance.receipt import DecisionReceipt
from raven.provenance.store import ReceiptEmitter, ReceiptStore

DEVNET = "https://api.devnet.solana.com"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
KEY_PATH = os.path.join(os.path.dirname(__file__), "..", "_keys", "raven-wallet.json")
JSONL_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "replay", "scores_historical_18222446.jsonl")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "receipts")
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Minimal sync Solana RPC + Memo anchor (solana 0.40 dropped sync Client)
# ---------------------------------------------------------------------------

def _rpc(method: str, params=None):
    r = _req.post(DEVNET, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data["result"]


class SyncMemoAnchor:
    """Sync Memo anchor using solders + raw HTTP RPC (no solana.rpc.api)."""

    backend = "memo"

    def __init__(self, keypair_path: str) -> None:
        with open(keypair_path) as f:
            secret = json.load(f)
        self._kp = Keypair.from_bytes(bytes(secret))
        self._memo_prog = Pubkey.from_string(MEMO_PROGRAM_ID)

    def anchor(self, receipt: DecisionReceipt) -> AnchorResult:
        h = receipt.commitment()
        try:
            memo_data = f"RAVEN:{h}".encode("utf-8")
            signer = self._kp.pubkey()

            ix = Instruction(
                program_id=self._memo_prog,
                data=memo_data,
                accounts=[AccountMeta(pubkey=signer, is_signer=True, is_writable=False)],
            )

            bh_result = _rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
            blockhash_str = bh_result["value"]["blockhash"]
            blockhash = Hash.from_string(blockhash_str)

            msg = MessageV0.try_compile(
                payer=signer,
                instructions=[ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self._kp])
            tx_bytes = base64.b64encode(bytes(tx)).decode()

            send_result = _rpc("sendTransaction", [tx_bytes, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}])
            sig = str(send_result)
            print(f"    ↗ anchored {h[:16]}… tx={sig[:20]}…")
            return AnchorResult(hash=h, signature=sig, anchored=True, backend=self.backend)

        except Exception as exc:
            print(f"    ⚠ anchor failed ({exc}); hash retained")
            return AnchorResult(hash=h, signature=None, anchored=False, backend=self.backend)


# ---------------------------------------------------------------------------
# Receipt writer callback
# ---------------------------------------------------------------------------

def make_receipt_writer(out_dir: str):
    """Returns an on_emit callback that writes each receipt to disk."""
    def on_emit(ar):
        payload = ar.receipt.to_payload()
        payload["receiptHash"] = ar.receipt_hash
        payload["anchorBackend"] = ar.anchor.backend
        payload["anchored"] = ar.anchor.anchored
        if ar.anchor.signature:
            payload["solanaTx"] = ar.anchor.signature
        seq = payload.get("txlineSequence", 0)
        path = os.path.join(out_dir, f"receipt_{seq}.json")
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
    return on_emit


# ---------------------------------------------------------------------------
# Main replay loop
# ---------------------------------------------------------------------------

def main():
    print("RAVEN full replay — real TxLINE data + Solana devnet anchoring")
    print("-" * 64)

    anchor = SyncMemoAnchor(KEY_PATH)
    store = ReceiptStore()
    on_emit = make_receipt_writer(OUT_DIR)
    emitter = ReceiptEmitter(anchor=anchor, store=store, on_emit=on_emit)
    agent = RavenAgent(emitter=emitter)

    n_frames = 0
    n_receipts = 0
    total_pnl = 0.0

    with open(JSONL_PATH) as fh:
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            frame = normalize(raw, fallback_sequence=i)
            result = agent.on_frame(frame)
            n_frames += 1
            total_pnl += result.realized_spread_pnl

            if result.receipt is not None:
                n_receipts += 1
                ar = result.receipt
                print(f"  {result.state.value:<12} seq #{frame.sequence:<6} {result.risk.reason[:50]}")

    print("-" * 64)
    print(f"frames={n_frames}  spread P&L={total_pnl:.4f}  receipts anchored={n_receipts}")

    # Save latest.json = last receipt written
    import glob, shutil
    files = sorted(glob.glob(os.path.join(OUT_DIR, "receipt_*.json")))
    if files:
        shutil.copy(files[-1], os.path.join(OUT_DIR, "latest.json"))
        print(f"latest.json -> {os.path.basename(files[-1])}")


if __name__ == "__main__":
    main()
