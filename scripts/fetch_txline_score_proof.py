#!/usr/bin/env python3
"""Fetch a public TxLINE Merkle proof for the packaged goal event."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ID = 18222446
SEQUENCE = 118
STAT_KEY = 1
OUTPUT = ROOT / "data/proofs/txline_score_18222446_seq118.json"


def _json_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> dict:
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_base = os.environ.get(
        "TXLINE_SSE_URL", "https://txline-dev.txodds.com/api"
    ).rstrip("/")
    api_token = os.environ.get("TXLINE_API_TOKEN", "")
    if not api_token:
        raise RuntimeError("TXLINE_API_TOKEN is required")
    origin = api_base[:-4] if api_base.endswith("/api") else api_base
    jwt = _json_request(
        f"{origin}/auth/guest/start",
        headers={"Content-Type": "application/json"},
        data=b"{}",
    )["token"]
    query = urllib.parse.urlencode(
        {"fixtureId": FIXTURE_ID, "seq": SEQUENCE, "statKey": STAT_KEY}
    )
    proof = _json_request(
        f"{api_base}/scores/stat-validation?{query}",
        headers={
            "Authorization": f"Bearer {jwt}",
            "X-Api-Token": api_token,
            "Accept": "application/json",
        },
    )
    artifact = {
        "network": "solana-devnet",
        "apiHost": origin,
        "programId": "6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J",
        "fixtureId": FIXTURE_ID,
        "sequence": SEQUENCE,
        "statKey": STAT_KEY,
        "expectedValue": 1,
        "validation": proof,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(f"Saved public TxLINE proof to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
