#!/usr/bin/env python3
"""
live_sim.py — Replay-as-Live simulator for RAVEN.

Streams scores_historical_18222446.jsonl at configurable speed,
runs full RAVEN pipeline + Solana devnet anchoring, prints a live
dashboard identical to what a real SSE feed would produce.

Usage:
    .venv/bin/python3 scripts/live_sim.py [--speed N] [--no-anchor]

    --speed N   frames per second (default: 10, use 0 for max speed)
    --no-anchor skip Solana anchoring (for offline demo)
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from raven.feed.normalize import normalize
from raven.agent import RavenAgent
from raven.provenance.anchor import AnchorResult
from raven.provenance.store import ReceiptEmitter, ReceiptStore

JSONL = os.path.join(os.path.dirname(__file__), "..", "data", "replay",
                     "scores_historical_18222446.jsonl")

# ── Noop anchor (offline mode) ────────────────────────────────────────────────
class NoopAnchor:
    backend = "noop"
    def anchor(self, receipt):
        return AnchorResult(hash=receipt.commitment(), signature=None,
                            anchored=False, backend="noop")

# ── Solana memo anchor (online mode) ─────────────────────────────────────────
def make_solana_anchor():
    from raven.provenance.anchor import SolanaAnchor
    import dotenv; dotenv.load_dotenv()
    return SolanaAnchor(
        rpc_url="https://api.devnet.solana.com",
        keypair_path=os.environ.get("SOLANA_KEYPAIR_PATH", "_keys/raven-wallet.json"),
        commitment="confirmed",
    )

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--speed", type=float, default=10.0,
                   help="frames/second (0 = max speed)")
    p.add_argument("--no-anchor", action="store_true",
                   help="skip Solana anchoring")
    args = p.parse_args()

    delay = (1.0 / args.speed) if args.speed > 0 else 0.0

    if args.no_anchor:
        anchor = NoopAnchor()
    else:
        try:
            anchor = make_solana_anchor()
            print("⚡ Solana devnet anchor: ACTIVE")
        except Exception as e:
            print(f"⚠️  Solana anchor unavailable ({e}), falling back to noop")
            anchor = NoopAnchor()

    store = ReceiptStore()
    receipts_emitted = []

    def on_emit(receipt):
        receipts_emitted.append(receipt)

    emitter = ReceiptEmitter(anchor=anchor, store=store, on_emit=on_emit)
    agent   = RavenAgent(emitter=emitter)

    print()
    print("═" * 68)
    print("  RAVEN LIVE-SIM — real TxLINE data, fixture 18222446")
    print(f"  speed={args.speed} fps  anchor={'noop' if args.no_anchor else 'solana-devnet'}")
    print("═" * 68)
    print()

    frames = shocks = receipts = 0
    pnl = 0.0
    t0 = time.time()

    with open(JSONL) as f:
        for i, line in enumerate(f, 1):
            raw = json.loads(line.strip())
            frame = normalize(raw, fallback_sequence=i)

            result = agent.on_frame(frame)
            frames += 1
            if frame.is_shock:
                shocks += 1
            if result.receipt is not None:
                receipts += 1
                tx = ""
                sig = getattr(result.receipt, "signature", None) or getattr(result.receipt, "solana_sig", None)
                if sig:
                    tx = f"  tx={str(sig)[:20]}…"
                state_str  = result.state.value
                reason_str = result.risk.reason[:52] if result.risk else ""
                marker = " ⚡" if getattr(result.receipt, "anchored", False) else " [noop]"
                print(f"  {state_str:<14} seq #{frame.sequence:<6} {reason_str}{tx}{marker}")

            if delay > 0:
                time.sleep(delay)

    elapsed = time.time() - t0
    print()
    print("─" * 68)
    print(f"  frames={frames}  shocks={shocks}  receipts={receipts}"
          f"  elapsed={elapsed:.1f}s")
    print(f"  Replay complete ✓ — real TxLINE fixture 18222446, seq 0..1306")
    print("─" * 68)

if __name__ == "__main__":
    main()
