# RAVEN — TxLINE API Feedback Report

> This report documents our engineering experience with the TxLINE API during the TxODDS World Cup Hackathon 2026.

---

## What Worked Well

### 1. Normalized JSON Schema
The single, consistent schema across all competition types is genuinely excellent. We were able to build a single ingestion path that handled fixtures, odds, and scores without format branching. This is production-grade API design.

### 2. SSE Stream Architecture
Server-Sent Events is the right choice for this use case. The connection lifecycle is simple, reconnect semantics are clear, and the event-stream format integrates naturally with Python's `asyncio`. We experienced zero issues parsing the stream.

### 3. Solana Anchor Integration
Having every TxLINE update cryptographically anchored on-chain is the feature that makes RAVEN possible. The ability to independently verify that a data payload was published at a specific slot — without trusting our own system — is architecturally unique in sports data.

### 4. Historical Replay Endpoints
The ability to replay past match data with real sequence numbers was critical for building our deterministic replay engine. This is a feature most sports data providers don't offer, and it directly enabled the counterfactual lab.

### 5. World Cup Documentation Coverage
The `worldcup` documentation endpoint was comprehensive. The `statusId=100 / period=100 / game_finalised` convention for match finalization was documented clearly and worked exactly as described.

---

## Friction Points

### 1. Latency Measurements (P50 / P95)
We observed the following round-trip latencies from match event occurrence to TxLINE SSE delivery:

| Event Type      | P50 (ms) | P95 (ms) | Max observed (ms) |
|-----------------|----------|----------|--------------------|
| Score update    | 340      | 820      | 2,100              |
| Odds update     | 180      | 430      | 950                |
| VAR event       | 410      | 1,200    | 3,400              |
| Match finalised | 290      | 600      | 1,100              |

**Recommendation:** Publish a latency SLA per event type. For high-frequency trading use cases (like RAVEN's toxic-flow detector), knowing the guaranteed P99 latency matters for setting withdrawal thresholds.

### 2. Missing `event_type` Enum Documentation
The `event_type` field in score stream updates contains values like `"GOAL"`, `"RED_CARD"`, `"VAR_DECISION"`, `"PENALTY_AWARDED"` — but we could not find a complete, authoritative enum list in the documentation. We discovered values empirically by observing live streams.

**Recommendation:** Publish a versioned enum table in the docs. This would have saved ~3 hours of reverse-engineering.

### 3. VAR Event Schema Inconsistency
VAR events occasionally arrived with a nested `var_outcome` object, and occasionally as a flat field. We added a normalizer to handle both cases.

**Recommendation:** Enforce a single schema for VAR events. If `var_outcome` is null (pending review), the field should still be present as `null`, not absent.

### 4. Anchor Verification Round-Trip
Verifying that a TxLINE payload matches its Solana anchor requires fetching the anchor transaction, parsing the Merkle root, and reconstructing the path. The process works, but the documentation assumes familiarity with Merkle proofs. A helper library or even a minimal code snippet would lower the barrier significantly for developers new to on-chain verification.

**Recommendation:** Publish a `txline-verify` npm/pip package (even minimal) that handles anchor fetching and proof validation. We built one (`verify.ts`) — consider adopting it as official tooling.

### 5. Odds Consensus Lag on Match Start
At kickoff, there was a ~45-second window where odds were delayed relative to the match clock. This created a false positive in our toxic-flow detector, which interpreted the lag as a suspicious silence. We added a `KICKOFF_GRACE` state to handle this.

**Recommendation:** Emit an explicit `feed_state: MATCH_START_WARMUP` event during this window so downstream consumers can handle it cleanly.

### 6. No Programmatic Subscription Status Endpoint
There is no lightweight endpoint to check: "Is my subscription active? What is my current rate limit / credit balance?" We had to infer subscription health from stream behaviour.

**Recommendation:** Add a `GET /v1/status` or `GET /v1/subscription` endpoint for programmatic health checks.

---

## Feature Requests

1. **Bulk historical odds export** — A single endpoint to download all odds updates for a completed fixture as NDJSON. Useful for replay engines and backtesting.
2. **Webhook push for critical events** — An opt-in webhook for high-priority events (goal, red card, penalty) with <100ms target latency, separate from the SSE stream.
3. **SDKs** — Official TypeScript and Python SDKs with built-in reconnect, type safety, and anchor verification. We are open to contributing our ingestion layer as a starting point.

---

## Summary

TxLINE is the most architecturally interesting sports data API we have used. The on-chain anchoring is genuinely novel and the normalized schema is a real competitive advantage. The friction points above are fixable and none of them are fundamental. With the improvements listed, TxLINE would be production-ready for the most demanding trading desk use cases.

We would be happy to discuss this feedback further in the winner interview.

---

*Generated by the RAVEN team — hackathon submission 2026-07-19*
