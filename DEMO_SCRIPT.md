# RAVEN Demo Script

Target length: 2-3 minutes. Speak the text below; the italic lines are actions.

## 1. Landing

*Open the deployed site.*

"Most market makers perform well until the match changes. RAVEN is built for
that exact moment. It is an autonomous market maker powered by TxLINE that
prices, protects, and proves every material risk action."

*Scroll briefly through System, Control Logic, and Measured Replay.*

"This comparison uses the same 1,976 real TxLINE odds, score, and event frames
for both policies. RAVEN reduces measured peak worst-case shock exposure by
87.76 percent versus an event-blind baseline."

## 2. Start The App

*Click **Enter Control Room**, then **Run replay**.*

"The review match has ended, so this is deterministic replay, not generated
market data. The prices are real TxLINE historical snapshots. Execution is
explicitly simulated because TxLINE is the data layer, not an order venue."

"RAVEN is now removing vig, deriving bounded fair value, and publishing
inventory-aware quotes across Match Winner, Asian Handicap, and Total Goals.
Fills update the portfolio, which feeds directly back into quote skew and risk."

## 3. Shock And Hedge

*Let the replay reach a withdrawal and hedge. Point to the posture, empty quote
book, exposure panel, and hedge result.*

"When risk becomes critical, RAVEN cancels the active quote book before it
hedges. The hedge engine evaluates the entire connected portfolio under home
goal, away goal, red card, and no-more-goals scenarios. It accepts a trade only
when the global worst case improves."

"In this first major hedge, worst-case exposure falls from 293.36 to 22.72.
RAVEN then waits in recalibration and re-enters only after stable consensus. No
manual action is required."

## 4. Proof

*Point to Decision Receipts and open the devnet proof link.*

"Each withdrawal, hedge, and re-entry receipt binds the exact TxLINE payload
hash, policy, inventory before and after, cancelled quotes, hedge trades, and
execution timestamp. These representative receipts are anchored to Solana
devnet and can be independently recomputed with `verify.ts`."

"We also validate TxLINE score sequence 118 against TxLINE's own devnet Merkle
root using their official program and IDL. So the proof chain covers both the
source event and RAVEN's autonomous response."

## 5. Close

"RAVEN is not a signal bot and it does not predict the winner. It is autonomous
market infrastructure designed to remain safe, auditable, and operational when
the market changes fastest."
