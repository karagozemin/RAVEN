#!/usr/bin/env python3
"""
reanchor_receipts.py — anchor any receipt that is missing solanaTx.

For each receipt_NNN.json that has anchored=False / solanaTx absent,
build a DecisionReceipt from the raw JSON, run MemoAnchor, and write
solanaTx + anchorBackend + anchored back into the file.

Usage:
    .venv/bin/python3 scripts/reanchor_receipts.py [--dry-run]
"""
import argparse, json, os, sys, glob, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dotenv; dotenv.load_dotenv()

from raven.provenance.anchor import MemoAnchor, AnchorResult
from raven.provenance.receipt import DecisionReceipt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="hash only, no on-chain write")
    args = p.parse_args()

    rpc = "https://api.devnet.solana.com"
    kp  = os.environ.get("SOLANA_KEYPAIR_PATH", "_keys/raven-wallet.json")

    if args.dry_run:
        from raven.provenance.anchor import NullAnchor
        anchor = NullAnchor()
        print("DRY RUN — no on-chain writes")
    else:
        anchor = MemoAnchor(rpc_url=rpc, keypair_path=kp, commitment="confirmed")
        print(f"MemoAnchor → {rpc}")

    files = sorted(glob.glob("receipts/receipt_*.json"))
    need  = [f for f in files if not json.load(open(f)).get("solanaTx")]
    skip  = len(files) - len(need)
    print(f"Found {len(files)} receipts — {skip} already anchored, {len(need)} need anchor\n")

    ok = fail = 0
    for path in need:
        raw = json.load(open(path))
        seq = raw.get("txlineSequence", path)
        try:
            receipt = DecisionReceipt.from_payload(raw)
            result: AnchorResult = anchor.anchor(receipt)
            if result.anchored and result.signature:
                raw["solanaTx"]      = result.signature
                raw["anchorBackend"] = result.backend
                raw["anchored"]      = True
                with open(path, "w") as f:
                    json.dump(raw, f, indent=2)
                print(f"  ✓ seq#{seq:<6}  tx={result.signature[:20]}…  [{path}]")
                ok += 1
            else:
                print(f"  ✗ seq#{seq:<6}  anchor returned no signature  [{path}]")
                fail += 1
        except Exception as e:
            print(f"  ✗ seq#{seq:<6}  ERROR: {e}  [{path}]")
            fail += 1
        time.sleep(0.5)   # avoid rate-limiting devnet RPC

    print(f"\nDone — {ok} anchored, {fail} failed")


if __name__ == "__main__":
    main()
