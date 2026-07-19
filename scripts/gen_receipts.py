#!/usr/bin/env python3
"""Generate receipt JSON files from the smoke-test pipeline for verify.ts.

Runs the same scripted frames as `python -m raven --smoke`, then saves each
anchored receipt to receipts/receipt_<seq>.json in the format verify.ts expects.
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from raven.agent import RavenAgent
from raven.feed.model import FrameKind, MatchEventType, OddsSnapshot, Score, VerifiedFrame

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "receipts")
os.makedirs(OUT_DIR, exist_ok=True)

# Mirror _smoke_frames() from main.py exactly
def smoke_frames():
    frames, seq, ts = [], 100, 1_784_412_000_000
    def odds(h, d, a): return OddsSnapshot(market="match_winner", outcomes={"home":h,"draw":d,"away":a})
    def frame(kind, *, o=None, event=MatchEventType.OTHER, score=None, clock="60:00"):
        nonlocal seq, ts
        seq += 1; ts += 1000
        return VerifiedFrame(kind=kind, fixture_id=17952170, sequence=seq,
                             timestamp_ms=ts, match_time=clock, score=score,
                             odds=o, event_type=event, payload_hash=f"hash{seq:04d}")
    frames.append(frame(FrameKind.ODDS, o=odds(1.90, 3.50, 4.20)))
    frames.append(frame(FrameKind.ODDS, o=odds(1.92, 3.45, 4.10)))
    frames.append(frame(FrameKind.ODDS, o=odds(1.88, 3.55, 4.30)))
    frames.append(frame(FrameKind.EVENT, event=MatchEventType.PENALTY_AWARDED, clock="67:14"))
    for _ in range(5):
        frames.append(frame(FrameKind.ODDS, o=odds(1.55, 3.90, 6.00), clock="68:00"))
    return frames

agent = RavenAgent()
for fr in smoke_frames():
    agent.on_frame(fr)

saved = 0
for result in agent.results:
    if result.receipt is None:
        continue
    ar = result.receipt
    payload = ar.receipt.to_payload()
    # Add anchor fields verify.ts needs
    payload["receiptHash"] = ar.receipt_hash
    payload["anchorBackend"] = ar.anchor.backend
    payload["anchored"] = ar.anchor.anchored
    if ar.anchor.signature:
        payload["solanaTx"] = ar.anchor.signature
    # verify.ts checks inventoryBefore/After as Record<string,number>
    # Python stores hashes; expose them as-is (TS just checks presence for required_fields)

    seq = payload.get("txlineSequence", saved)
    path = os.path.join(OUT_DIR, f"receipt_{seq}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    print(f"  saved {path}  action={payload['action']}")
    saved += 1

# Also save latest.json pointing to last receipt
if saved:
    import shutil
    latest = os.path.join(OUT_DIR, "latest.json")
    shutil.copy(path, latest)
    print(f"  saved {latest}")

print(f"\nGenerated {saved} receipt files in {os.path.abspath(OUT_DIR)}/")
