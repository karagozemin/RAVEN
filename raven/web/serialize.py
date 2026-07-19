"""JSON serialization for the Web Control Room.

Turns the agent's rich domain objects (:class:`~raven.agent.TickResult`, quotes,
hedge plans, receipts) into plain JSON-safe dicts the browser can render. Kept
separate from both the agent (which stays pure) and the server (which stays
transport-only) so the wire format has a single, testable home.

The wire schema is intentionally flat and self-describing: every message is a
``{"type": ..., "data": {...}}`` envelope so the frontend can switch on ``type``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from raven.agent import TickResult
from raven.hedging.engine import HedgePlan
from raven.provenance.store import AnchoredReceipt
from raven.quoting.engine import QuoteSet
from raven.quoting.inventory import Inventory


def _round(x: Optional[float], n: int = 6) -> Optional[float]:
    if x is None:
        return None
    return round(float(x), n)


def quotes_to_json(quotes: Dict[str, QuoteSet]) -> List[Dict[str, Any]]:
    """Flatten every live outcome quote into a list of rows for the table."""
    rows: List[Dict[str, Any]] = []
    for market, qs in quotes.items():
        for outcome, q in qs.quotes.items():
            if q.withdrawn:
                continue
            mid = q.mid
            rows.append(
                {
                    "market": market,
                    "outcome": outcome,
                    "fair": _round(q.fair),
                    "bid": _round(q.bid),
                    "ask": _round(q.ask),
                    "mid": _round(mid),
                    "spread": _round(q.spread),
                    "spread_pct": _round(q.spread / mid * 100.0, 3) if mid else 0.0,
                    "bid_size": _round(q.bid_size, 2),
                    "ask_size": _round(q.ask_size, 2),
                }
            )
    return rows


def hedge_to_json(plan: Optional[HedgePlan]) -> Optional[Dict[str, Any]]:
    if plan is None or plan.is_noop:
        return None
    return {
        "trades": [
            {
                "market": t.market,
                "outcome": t.outcome,
                "side": t.side,
                "size": _round(t.size, 2),
                "price": _round(t.price),
            }
            for t in plan.trades
        ],
        "worst_before": _round(plan.worst_before.delta, 2),
        "worst_after": _round(plan.worst_after.delta, 2),
        "reduction": _round(plan.reduction, 2),
        "residual": _round(plan.residual, 2),
        "worst_shock": plan.worst_before.shock.value,
    }


def receipt_to_json(anchored: Optional[AnchoredReceipt]) -> Optional[Dict[str, Any]]:
    if anchored is None:
        return None
    r = anchored.receipt
    a = anchored.anchor
    return {
        "hash": anchored.receipt_hash,
        "action": r.action.value,
        "reason": r.reason,
        "sequence": r.txline_sequence,
        "new_state": r.new_state,
        "previous_state": r.previous_state,
        "risk_score": _round(r.risk_score, 4),
        "quotes_cancelled": r.quotes_cancelled,
        "hedge_trades": len(r.hedge_trades),
        "signature": a.signature,
        "anchored": bool(a.anchored),
        "backend": a.backend,
    }


def exposure_to_json(inventory: Optional[Inventory]) -> List[Dict[str, Any]]:
    """Every open position as a signed-notional row for the exposure panel."""
    if inventory is None:
        return []
    rows: List[Dict[str, Any]] = []
    for pos in inventory:
        if pos.is_flat:
            continue
        rows.append(
            {
                "market": pos.market,
                "outcome": pos.outcome,
                "quantity": _round(pos.quantity, 2),
                "avg_price": _round(pos.avg_price),
                "notional": _round(pos.notional(), 2),
                "side": "long" if pos.quantity > 0 else "short",
            }
        )
    return rows


def tick_to_json(
    result: TickResult,
    *,
    tick_index: int,
    inventory: Optional[Inventory] = None,
) -> Dict[str, Any]:
    """Serialize a single :class:`TickResult` into a wire message.

    ``inventory`` is the agent's live book *after* this tick; it is passed in
    (rather than read off the frozen result) so the exposure panel reflects the
    current portfolio, including any hedge fills applied on this tick.
    """
    f = result.frame
    d = result.risk

    score = f.score
    odds = None
    if f.odds is not None:
        odds = {k: _round(v, 2) for k, v in f.odds.outcomes.items()}

    quotes = quotes_to_json(result.quotes)

    return {
        "tick": tick_index,
        "sequence": f.sequence,
        "timestamp_ms": f.timestamp_ms,
        "kind": f.kind.value,
        "fixture_id": f.fixture_id,
        "match_time": f.match_time,
        "event_type": f.event_type.value,
        "is_shock": f.is_shock,
        "is_final": f.is_final,
        "verified": f.verified,
        "provenance": f.short_provenance(),
        "score": {
            "home": score.home if score else 0,
            "away": score.away if score else 0,
        },
        "odds": odds,
        "state": d.state.value,
        "prior_state": d.prior_state.value,
        "transitioned": d.transitioned,
        "triggered_by_shock": d.triggered_by_shock,
        "risk_score": _round(d.risk_score * 100.0, 1),
        "reason": d.reason,
        "is_quoting": result.is_quoting,
        "quotes": quotes,
        "quotes_count": len(quotes),
        "spread_pnl": _round(result.realized_spread_pnl, 6),
        "hedge": hedge_to_json(result.hedge),
        "receipt": receipt_to_json(result.receipt),
        "exposure": exposure_to_json(inventory),
    }
