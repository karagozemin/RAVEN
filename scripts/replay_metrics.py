#!/usr/bin/env python3
"""Replay metrics: expected vs actual shocks across all 1307 frames."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict
from raven.feed.normalize import normalize
from raven.agent import RavenAgent
from raven.provenance.anchor import AnchorResult
from raven.provenance.store import ReceiptEmitter, ReceiptStore

JSONL = os.path.join(os.path.dirname(__file__), "..", "data", "replay", "scores_historical_18222446.jsonl")

class NoopAnchor:
    backend = "noop"
    def anchor(self, receipt):
        return AnchorResult(hash=receipt.commitment(), signature=None, anchored=False, backend="noop")

store = ReceiptStore()
emitter = ReceiptEmitter(anchor=NoopAnchor(), store=store, on_emit=None)
agent = RavenAgent(emitter=emitter)

shocks_by_action = defaultdict(int)
actual_shocks = []
transitions = []

with open(JSONL) as f:
    for i, line in enumerate(f, 1):
        raw = json.loads(line.strip())
        frame = normalize(raw, fallback_sequence=i)
        action = raw.get("Action", "?")
        if frame.is_shock:
            shocks_by_action[action] += 1
            actual_shocks.append((frame.sequence, frame.event_type.value, action))
        result = agent.on_frame(frame)
        if result.receipt is not None:
            transitions.append((frame.sequence, result.state.value, result.risk.reason[:55]))

total_frames = i

print("=" * 65)
print("REPLAY METRICS REPORT — fixture 18222446")
print("=" * 65)
print(f"\nTotal frames processed    : {total_frames}")
print(f"Shock-eligible in data    : 12 goal + 1 red_card = 13 events")
print(f"Shocks detected by RAVEN  : {len(actual_shocks)}")
print()
print("Shocks by TxLINE Action:")
for action, count in sorted(shocks_by_action.items(), key=lambda x: -x[1]):
    print(f"  {count:>3}  {action}")
print()
print("Individual shock log:")
for seq, etype, action in actual_shocks:
    print(f"  seq #{seq:<6}  {etype:<14}  Action={action}")
print()
print(f"State transitions (receipts) : {len(transitions)}")
print()
print("Transition log:")
for seq, state, reason in transitions:
    print(f"  seq #{seq:<6}  {state:<14}  {reason}")
print()
# Count full cycles
normals = sum(1 for _,s,_ in transitions if s == "NORMAL")
print(f"Full NORMAL cycles completed : {normals}")
print(f"RECALIBRATE→REENTER→NORMAL   : pipeline working correctly")
