# RAVEN - TxLINE API Feedback

This report documents the integration work completed for the TxODDS World Cup
Hackathon 2026. It intentionally avoids latency or reliability claims that were
not measured under a controlled methodology.

## What Worked Well

### Normalized domain model

TxLINE's consistent fixture, participant, score, stat, and market concepts made
it possible to keep one normalization boundary for three connected markets:
Match Winner, Asian Handicap, and Total Goals. Once normalized, the pricing and
risk layers no longer need competition-specific adapters.

### Separate odds and score streams

The documented SSE endpoints map well to an event-driven trading architecture:

- `/api/odds/stream`
- `/api/scores/stream`

RAVEN runs both concurrently, merges them through an async queue, records raw
payloads before transformation, and reconnects with bounded backoff.

### Historical score sequences

`/api/scores/historical/{fixtureId}` preserved real native `Seq` values. This
was essential for replay provenance and for requesting a score-stat validation
proof for an observed record rather than using a fabricated sequence.

### Public on-chain validation

The `/api/scores/stat-validation` response plus the public devnet IDL and
program address made independent verification possible. RAVEN validates fixture
`18222446`, score sequence `118`, stat key `1`, value `1` against TxLINE's
`daily_scores_roots` PDA using a read-only `validateStat` simulation.

### Explicit finalization semantics

The documented `game_finalised`, `statusId=100`, and `period=100` conventions
provide a clear settlement boundary across regulation time, extra time, and
other terminal outcomes.

## Integration Friction

### Credential and network coupling

Every request needs both a guest Bearer JWT and `X-Api-Token`. The JWT, token,
API host, Solana cluster, subscription transaction, and program ID must all
belong to the same network. This is correct from a security perspective, but it
creates several ways for a first integration to fail with a generic `401` or
`403`.

Recommendation: add a single authenticated diagnostics endpoint that reports
network, subscription tier, token status, JWT expiry, and enabled products
without exposing secret material.

### Historical score and odds asymmetry

Scores can be fetched by fixture. The historical odds path used by RAVEN is
organized into epoch-day/hour/five-minute intervals, so obtaining a completed
fixture requires deriving time buckets, downloading them in parallel, filtering
by `FixtureId`, market type, period, and line, then deduplicating records.

Recommendation: add a fixture-scoped odds NDJSON export such as
`/api/odds/historical/{fixtureId}` with optional market and line filters.

### Sequence semantics differ by product

Historical score records expose native `Seq`; historical odds interval records
do not. A merged replay therefore cannot safely treat one integer as both global
ordering and provider provenance.

Recommendation: expose a documented provider sequence or stable message ID on
all historical odds records, and state whether ordering is global, per fixture,
per market, or per stream.

### Schema casing and partial event enrichment

The native records use PascalCase fields such as `FixtureId`, `PriceNames`,
`Prices`, and `Score`, while examples and client-side objects may expose camel
case. Score events can also arrive as a short event followed by records enriched
with goal type and player ID. This is manageable, but easy to misinterpret as
multiple independent goals.

Recommendation: publish generated JSON Schema files for stream and historical
payloads, including the event enrichment lifecycle and deduplication key.

### On-chain validation setup is powerful but heavy

The validation flow requires the correct IDL, generated types, program ID,
network host, proof conversion to 32-byte arrays, epoch-day PDA derivation, and
an Anchor view call. The documentation now covers these pieces, but a consumer
still assembles substantial boilerplate before validating one stat.

Recommendation: publish an official `@txline/verify` package exposing a small
API such as `verifyScoreStat({ fixtureId, seq, statKey, predicate })`.

### End-to-end production examples

The individual endpoint examples are useful. The remaining onboarding gap is
understanding how live streams, historical replay, proof validation, reconnect,
and production state recovery fit together.

Recommendation: add one end-to-end reference consumer that records both streams,
detects gaps, rehydrates from historical endpoints, and validates one observed
score record on-chain.

## Requested Improvements

1. Fixture-scoped bulk historical odds export.
2. Subscription/token diagnostics endpoint.
3. Official typed Python and TypeScript clients with reconnect and JWT refresh.
4. Official on-chain verification helper library.
5. Versioned schemas and enum tables for score actions, phases, and market types.
6. Production replay example covering ordering and recovery after disconnect.

## Summary

TxLINE's strongest architectural qualities are normalized sports data, separate
real-time products, replayable native score sequences, and public Solana roots.
Those capabilities let RAVEN spend its complexity budget on pricing, portfolio
risk, and auditability. The largest opportunity is not a new primitive; it is a
thinner, typed onboarding layer that connects the primitives already available.
