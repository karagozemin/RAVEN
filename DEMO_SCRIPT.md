# RAVEN Demo Video Script

Target length: approximately 2 minutes. Speak calmly and let the state transition
remain visible long enough for the viewer to follow it.

## Before Recording

- Open the deployed Control Room and verify that the backend is connected.
- Reset or reload the replay so the video begins in `NORMAL`.
- Keep the state, quote table, risk score, exposure, and receipt feed visible.
- Close unrelated tabs and hide credentials, wallet files, and environment variables.

## Narration

### 0:00-0:15 — Problem and Product

**On screen:** Show the RAVEN logo and the full Control Room.

> This is RAVEN, the Real-time Autonomous Verifiable Exposure Neutralizer. It is
> an event-aware market-making agent for live football markets. Most market makers
> earn spread during normal play, but fail when a goal, red card, penalty, or VAR
> decision moves every connected market at once. RAVEN is designed for that moment.

### 0:15-0:35 — Verified Input and Normal Quoting

**On screen:** Point to the TxLINE sequence, score, clock, state, and live quotes.

> The demo is replaying recorded TxLINE match data through the same normalized
> frame pipeline used by live ingestion. On every update, RAVEN removes the vig,
> estimates a bounded fair value, and publishes inventory-aware bid and ask quotes.
> In the NORMAL state, the agent is actively earning spread while continuously
> measuring latency, volatility, consensus deviation, and portfolio exposure.

### 0:35-1:05 — Event Shock

**On screen:** Let the critical event arrive. Highlight the state badge and the
disappearing quote rows.

> Now a critical match event arrives. RAVEN does not try to predict through the
> shock. The deterministic risk kernel moves immediately into WITHDRAW and removes
> its quotes, so informed flow cannot trade against stale prices. The state then
> advances to HEDGE, where RAVEN evaluates the entire connected inventory under
> multiple event scenarios and selects trades that reduce the worst exposure.

### 1:05-1:25 — Controlled Recovery

**On screen:** Show `RECALIBRATE`, then `REENTER`, and finally fresh quotes.

> RAVEN does not re-enter after a timer alone. It waits in RECALIBRATE for
> consecutive stable updates from the market. Only after consensus settles does it
> move through REENTER and safely publish fresh two-sided quotes around the new
> market state.

### 1:25-1:45 — Verifiable Decisions

**On screen:** Highlight a receipt entry and its sequence, reason, state, and hash.

> Every material state transition, withdrawal, and hedge produces a canonical
> decision receipt. The receipt binds the TxLINE sequence and market-state hash to
> the risk score, action, reason, inventory state, and hedge details. Receipts are
> hash-addressed locally and can be anchored to Solana devnet for independent
> verification with the included TypeScript verifier.

### 1:45-2:05 — Architecture and Close

**On screen:** Briefly show the architecture diagram, then return to the dashboard.

> The system is deliberately modular: TxLINE ingestion, fair-value pricing, quote
> generation, the risk state machine, cross-market hedging, and provenance are
> separate components. Live data and replay data enter the same decision core, so
> the behaviour you see here is reproducible rather than hard-coded for the demo.
> RAVEN has one objective: capture spread in normal markets, survive event shocks,
> and prove exactly why every material decision was made.

## Short 60-Second Version

> This is RAVEN, an event-aware market-making agent for live football. It consumes
> TxLINE data, removes the vig, estimates bounded fair value, and publishes
> inventory-aware two-sided quotes. The key moment is a verified match shock. When
> this event arrives, RAVEN immediately enters WITHDRAW and removes stale liquidity.
> It then evaluates the connected portfolio, moves into HEDGE, and reduces the worst
> event exposure. Instead of re-entering on a fixed timer, RAVEN waits for consecutive
> stable market updates, then publishes fresh quotes in REENTER. Every material
> transition creates a canonical receipt containing the TxLINE sequence, source hash,
> risk score, action, reason, and hedge details. Those receipts can be anchored to
> Solana devnet and independently checked with our TypeScript verifier. Live and
> recorded TxLINE frames use the same deterministic pipeline, making this demo fully
> reproducible. RAVEN earns during normal flow, survives the shock, and proves every
> decision.

## One-Line Closing

> RAVEN is not trying to predict the next goal; it is making sure the market maker
> survives it.
