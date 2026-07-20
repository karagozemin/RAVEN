#!/usr/bin/env python3
"""Anchor three deterministic demo decisions and publish their public proofs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from raven.agent import RavenAgent
from raven.provenance.anchor import MemoAnchor
from raven.web.driver import iter_verified_frames

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "receipts/anchored_demo.json"


def _keypair_path() -> str:
    return os.environ.get("SOLANA_KEYPAIR_PATH") or str(
        ROOT / "_keys/raven-wallet.json"
    )


def main() -> int:
    agent = RavenAgent()
    selected = {}
    for frame in iter_verified_frames():
        result = agent.on_frame(frame)
        anchored = result.receipt
        if anchored is None:
            continue
        receipt = anchored.receipt
        is_txline_goal = frame.verified and receipt.action.value == "WITHDRAW"
        if (
            receipt.action.value == "WITHDRAW"
            and receipt.quotes_cancelled > 0
            and not is_txline_goal
        ):
            selected.setdefault("withdraw", receipt)
        if is_txline_goal:
            selected.setdefault("txline_goal_withdraw", receipt)
        elif receipt.action.value == "CANCEL_AND_HEDGE" and receipt.hedge_trades:
            selected.setdefault("hedge", receipt)
        elif receipt.new_state == "REENTER" and "hedge" in selected:
            selected.setdefault("reenter", receipt)
        if len(selected) == 4:
            break

    if len(selected) != 4:
        raise RuntimeError(f"Expected four material decisions, found {selected.keys()}")

    existing = {}
    if OUTPUT.exists():
        archive = json.loads(OUTPUT.read_text(encoding="utf-8"))
        existing = {
            item.get("label"): item
            for item in archive.get("receipts", [])
            if isinstance(item, dict)
        }

    anchor = MemoAnchor(
        rpc_url="https://api.devnet.solana.com",
        keypair_path=_keypair_path(),
    )
    proofs = []
    for label in ("withdraw", "hedge", "txline_goal_withdraw", "reenter"):
        receipt = selected[label]
        prior = existing.get(label)
        if prior and prior.get("receiptHash") == receipt.hash():
            signature = prior["solanaTx"]
            backend = prior.get("anchorBackend", "memo")
        else:
            result = anchor.anchor(receipt)
            if not result.anchored or not result.signature:
                raise RuntimeError(f"Failed to anchor {label}")
            signature = result.signature
            backend = result.backend
        proofs.append(
            {
                "label": label,
                **receipt.to_payload(),
                "receiptHash": receipt.hash(),
                "solanaTx": signature,
                "anchorBackend": backend,
                "anchored": True,
            }
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "network": "solana-devnet",
                "memoProgram": MemoAnchor.MEMO_PROGRAM_ID,
                "receipts": proofs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(proofs)} verified public proofs to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
