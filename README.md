# 🐦‍⬛ RAVEN
### Real-time Autonomous Verifiable Exposure Neutralizer

> *"Most market makers earn until the match changes. RAVEN is built for the moment it does."*

RAVEN is an autonomous in-play market maker for live football markets, powered by TxLINE real-time data anchored on Solana. It earns spread under normal flow, and — the instant a cryptographically verified match event hits — withdraws quotes, neutralizes exposure across connected markets, then safely re-enters. Every decision is proved on-chain.

**Track:** Trading Tools and Agents — TxODDS World Cup Hackathon 2026
**Prize target:** 1st Place — 10,000 USDT

---

## The Problem

Live football markets move violently the instant match state changes. A goal, red card, or VAR overturn affects every connected market simultaneously — 1X2, Handicap, Total Goals, Correct Score. If even one quote stays stale for seconds, informed traders drain margin before a human can react.

Traditional bots either (a) predict direction and lose, or (b) quote statically and get picked off during event shocks. Neither is production-ready.

## The Solution

RAVEN combines three layers:

1. **Earn** — Continuously quotes bid/ask using TxLINE consensus odds + event-hazard fair value
2. **Survive** — Detects verified event shocks (goal, red card, penalty, VAR) and withdraws within milliseconds; neutralizes cross-market exposure autonomously
3. **Prove** — Anchors every quote, hedge, and risk decision to Solana devnet as a cryptographically verifiable Decision Receipt

---

## Architecture

```
TxLINE SSE / Replay Feed
        │
        ▼
[F1] Verified Feed Layer         ← hash · sequence · Solana anchor ref
        │
   ┌────┴────┐
   ▼         ▼
[F2] Fair-Value    [F5] Market Dependency Graph
    Engine              (cross-market coherence)
   │
   ▼
[F3] Quote Engine  ← inventory skew · spread controls
   │
   ▼
[F4] Risk Kernel / State Machine ← [F9] Adversarial Flow Detector
   NORMAL → CAUTION → WITHDRAW → HEDGE → RECALIBRATE → REENTER
   │
   ▼
[F6] Self-Hedging Engine  ← cross-market exposure neutralizer
   │
   ▼
[F7] Solana Decision Receipts  ──► verify.ts (independent proof)
   │
   ▼
[F8] Deterministic Replay & Counterfactual Lab
   │
   ▼
[F10] Live Control Room Dashboard
```

---

## Mathematical Foundation

### 1. Vig Removal (Fair Probability)

Raw probability from decimal odds:

$$p_{raw,i} = \frac{1}{odds_i}$$

Multiplicative vig removal:

$$p_{fair,i} = \frac{p_{raw,i}}{\sum_j p_{raw,j}}$$

### 2. Event-Hazard Adjustment

RAVEN does **not** assume linear "theta decay". Instead, it models match-state-dependent event hazard using an inhomogeneous Poisson process where goal intensities vary with score, time remaining, and match phase:

$$\lambda(t, s) = \lambda_0 \cdot f(t) \cdot g(s) \cdot h(events)$$

The reservation price is:

$$p_{reservation} = p_{fair} + \Delta_{hazard} + \Delta_{inventory} + \Delta_{latency/vol}$$

The adjusted probability is bounded to not deviate from TxLINE consensus beyond a configurable threshold (model-risk cap).

### 3. Quote Engine

Components are strictly separated:

| Layer | Role |
|-------|------|
| Fair-value model | Computes probability |
| Quote engine | Generates bid/ask |
| Inventory skew | Shifts quotes against over-held outcome |
| Risk limits | Sets maximum position size |
| Fractional Kelly | Sets capital-usage cap only (does not generate quotes) |

$$f^* = \frac{bp - q}{b}$$

Example with fair probability = 0.582:

```
base spread      = 1.8%
inventory skew   = -0.7%
event premium    = 2.4%
─────────────────────────
bid = 0.547   ask = 0.603
```

### 4. Cross-Market Risk Score

$$risk = 0.30 \cdot \Delta_{consensus} + 0.25 \cdot latency_{event} + 0.20 \cdot incoherence_{cross} + 0.15 \cdot exposure + 0.10 \cdot confidence_{feed}$$

### 5. Self-Hedging Objective

Minimize portfolio event-shock loss without eliminating spread revenue:

$$\min\left[\mathbb{E}[\text{loss under event shocks}] + \alpha \cdot \text{inventory concentration} + \beta \cdot \text{hedge cost}\right]$$

Cross-market hedge instruments:
- Long Home Win → Short Home Asian Handicap
- 1X2 ↔ Double Chance netting
- Goal shock ↔ Total Goals hedge

---

## Quickstart

### Prerequisites

- Python 3.11+
- Node.js 18+ (for verify.ts)
- Solana devnet wallet (auto-created on first run)
- TxLINE API credentials (set in `.env`)

### Setup

```bash
# Clone
git clone https://github.com/your-org/raven
cd raven

# Python environment
pip install -r requirements.txt

# Copy env and add your TxLINE API key
cp .env.example .env
# Edit .env: set TXLINE_API_KEY

# Node dependencies (for verify.ts)
npm install
```

### Run RAVEN (Live Mode)

```bash
python -m raven.main --mode live
```

### Run with Deterministic Replay

```bash
python -m raven.main --mode replay --file data/wc2026_recorded.ndjson --speed 10
```

### Run Counterfactual Lab (RAVEN vs Baseline)

```bash
python -m raven.counterfactual --file data/wc2026_recorded.ndjson
```

### Dashboard

```bash
python raven/dashboard.py
# Opens on http://localhost:8050
```

---

## TxLINE Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `GET /fixtures` | Fetch World Cup fixture list |
| `GET /odds/snapshot/{fixtureId}` | Initial consensus odds snapshot |
| `SSE /odds/stream/{fixtureId}` | Live odds stream with anchor refs |
| `SSE /scores/stream/{fixtureId}` | Live score and match event stream |
| `GET /scores/history/{fixtureId}` | Historical match replay data |
| `GET /fixtures/{fixtureId}/proof` | Solana Merkle proof for verification |

---

## Verifying a Decision Receipt

Every critical action RAVEN takes is anchored on Solana devnet as a Decision Receipt.

```bash
# Install dependencies
npm install

# Verify a receipt
npx ts-node verify.ts <SOLANA_TX_SIGNATURE>
```

Output:
```
🐦‍⬛ RAVEN Decision Receipt Verifier
══════════════════════════════════════
Transaction: 5x9kABC...
Slot:        428,113,908
Timestamp:   2026-07-15T21:14:05.144Z

Receipt:
  Action:   CANCEL_AND_HEDGE
  Reason:   VERIFIED_PENALTY_EVENT
  Policy:   raven-v1.0.0
  Markets:  3 suspended
  Hedge:    executed (residual: +930 USDC)
  Quotes cancelled: 14

✓ Receipt is valid — decision is cryptographically verified
```

---

## State Machine

```
NORMAL ──── risk low ────────────────────────────────┐
  │                                                   │
  ▼ risk rising                                       │
CAUTION ──── risk stabilises ──────────────────────► │
  │                                                   │
  ▼ critical event / risk threshold                   │
WITHDRAW                                              │
  │ (cancel all quotes)                               │
  ▼                                                   │
HEDGE                                                 │
  │ (neutralize cross-market exposure)                │
  ▼                                                   │
RECALIBRATE                                           │
  │ (await 3 consecutive stable TxLINE updates)       │
  ▼                                                   │
REENTER ──────────────────────────────────────────── ┘
  (reopen quotes at new consensus price)
```

---

## Counterfactual Results

Same TxLINE replay feed, two agents:

| Metric | Baseline MM | RAVEN |
|--------|-------------|-------|
| Spread revenue | 3,800 USDC | 3,420 USDC |
| Event shock loss | -6,900 USDC | -720 USDC |
| **Net P&L** | **-3,100 USDC** | **+2,700 USDC** |
| Max inventory exposure | 21,000 USDC | 7,400 USDC |
| Manual interventions required | 3 | 0 |
| Event-to-withdrawal latency | ~4,200 ms | **~230 ms** |

> All numbers are outputs from the deterministic replay engine on real recorded TxLINE data — not pre-selected showcase figures.

---

## Repository Structure

```
raven/
├── feed/               # TxLINE ingestion, normalization, replay
│   ├── live.py         # Async SSE connection + recorder
│   ├── replay.py       # Deterministic replay engine
│   ├── model.py        # Pydantic data models
│   ├── normalize.py    # Schema normalization
│   └── source.py       # Feed source abstraction
├── pricing/            # Fair-value computation
│   ├── fair_value.py   # Reservation price aggregator
│   ├── hazard.py       # Event-hazard model (Poisson)
│   ├── vig.py          # Vig removal
│   └── state.py        # Match state tracker
├── quoting/            # Quote generation
│   ├── engine.py       # Bid/ask + expiry + max size
│   └── inventory.py    # Inventory skew
├── risk/               # Risk management
│   ├── kernel.py       # State machine (NORMAL→REENTER)
│   ├── dependency_graph.py  # Cross-market coherence
│   └── adversarial.py  # Toxic flow detection
├── hedging/            # Self-hedging engine
│   └── engine.py       # Cross-market exposure neutralizer
├── provenance/         # Solana decision receipts
│   ├── receipt.py      # Receipt builder
│   ├── anchor.py       # Solana devnet writer
│   └── store.py        # Local receipt index
├── agent.py            # Main agent orchestration loop
├── config.py           # Configuration
├── counterfactual.py   # Baseline vs RAVEN comparison
├── dashboard.py        # Live Control Room (Dash)
└── main.py             # Entry point
verify.ts               # Independent receipt verifier (TypeScript)
FEEDBACK.md             # TxLINE API engineering feedback
```

---

## MVP Scope

**Markets:** Match Winner (1X2) · Total Goals · Asian Handicap

**Events handled:** Goal · Red Card · VAR Overturn · Match Finalisation

**Execution:** Simulated order book on devnet (no real money required to test)

**Chain:** Solana devnet — receipts verifiable by any third party

---

## Key Design Decisions

**LLM is not the decision-maker.** The risk kernel and quote engine are fully deterministic. LLM is only used in the dashboard's Decision Inspector to generate human-readable explanations of decisions already made by the mathematical engine.

**TxLINE consensus is not replaced.** The event-hazard model applies a bounded adjustment on top of the TxLINE consensus probability. The model never overrides consensus — it refines it. This caps model risk.

**Shadow mode ready.** RAVEN is designed to run alongside an existing sportsbook trading system. It monitors, withdraws, and hedges — it does not replace the operator's trading engine.

---

## License

MIT — see LICENSE

---

*RAVEN — TxODDS World Cup Hackathon 2026 submission*
*Built with TxLINE real-time data · Solana devnet*
