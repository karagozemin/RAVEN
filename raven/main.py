"""RAVEN entry point (F1→F2→F3→F4→F6→F7).

Runs the agent against a feed source — live TxLINE SSE, or a replay of
previously recorded *real* TxLINE bytes (F8). Selection is driven entirely by
:class:`~raven.config.Settings` so the same command works in either mode:

    python -m raven.main            # uses RAVEN_FEED_MODE from .env
    python -m raven.main --smoke    # self-contained deterministic smoke test

The ``--smoke`` path exercises the whole pipeline end-to-end on a tiny scripted
sequence of frames (real schema, synthetic values) so we can prove every layer
imports and composes correctly without a network or a live match.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import AsyncIterable, Iterable, List

from raven.agent import RavenAgent, TickResult
from raven.config import load_settings
from raven.feed.model import (
    FrameKind,
    MatchEventType,
    OddsSnapshot,
    Score,
    VerifiedFrame,
)


def _render(result: TickResult) -> None:
    """One compact CLI line per tick for the Control Room / demo."""
    f = result.frame
    tag = result.state.value.ljust(11)
    seq = f"seq #{f.sequence}".ljust(10)
    extra = ""
    if result.hedge is not None and not result.hedge.is_noop:
        extra = (
            f"  hedge: {len(result.hedge.trades)} trade(s) "
            f"worst {result.hedge.worst_before.delta:+.0f}"
            f" -> {result.hedge.worst_after.delta:+.0f}"
        )
    elif result.is_quoting:
        extra = f"  spread P&L +{result.realized_spread_pnl:.4f}"
    recv = ""
    if result.receipt is not None:
        proof = "devnet" if result.receipt.anchor.anchored else "local"
        recv = f"  [receipt · {proof}]"
    print(f"{tag} {seq} {result.risk.reason}{extra}{recv}")


def _smoke_frames() -> List[VerifiedFrame]:
    """A scripted arc: calm quoting → verified penalty shock → recovery.

    Uses the real frame schema; values are synthetic. The penalty frame is a
    shock, forcing WITHDRAW→HEDGE, then a run of stable odds lets the kernel
    RECALIBRATE→REENTER→NORMAL.
    """
    frames: List[VerifiedFrame] = []
    seq = 100
    ts = 1_784_412_000_000

    def odds(home: float, draw: float, away: float) -> OddsSnapshot:
        return OddsSnapshot(
            market="match_winner",
            outcomes={"home": home, "draw": draw, "away": away},
        )

    def frame(
        kind: FrameKind,
        *,
        o: OddsSnapshot | None = None,
        event: MatchEventType = MatchEventType.OTHER,
        score: Score | None = None,
        clock: str = "60:00",
    ) -> VerifiedFrame:
        nonlocal seq, ts
        seq += 1
        ts += 1000
        return VerifiedFrame(
            kind=kind,
            fixture_id=17952170,
            sequence=seq,
            timestamp_ms=ts,
            match_time=clock,
            score=score,
            odds=o,
            event_type=event,
            payload_hash=f"hash{seq:04d}",
        )


    # Calm: a few odds refreshes → NORMAL quoting.
    frames.append(frame(FrameKind.ODDS, o=odds(1.90, 3.50, 4.20)))
    frames.append(frame(FrameKind.ODDS, o=odds(1.92, 3.45, 4.10)))
    frames.append(frame(FrameKind.ODDS, o=odds(1.88, 3.55, 4.30)))
    # Verified penalty shock → WITHDRAW → HEDGE.
    frames.append(
        frame(
            FrameKind.EVENT,
            event=MatchEventType.PENALTY_AWARDED,
            clock="67:14",
        )
    )
    # Post-shock stable odds → RECALIBRATE → REENTER → NORMAL.
    for _ in range(5):
        frames.append(frame(FrameKind.ODDS, o=odds(1.55, 3.90, 6.00), clock="68:00"))
    return frames


def run(frames: Iterable[VerifiedFrame]) -> int:
    agent = RavenAgent(on_tick=_render)
    n = 0
    for fr in frames:
        agent.on_frame(fr)
        n += 1
    total_pnl = sum(r.realized_spread_pnl for r in agent.results)
    receipts = sum(1 for r in agent.results if r.receipt is not None)
    print("-" * 60)
    print(f"ticks={n}  spread opportunity={total_pnl:.4f}  receipts={receipts}")
    return 0


async def run_async(frames: AsyncIterable[VerifiedFrame]) -> int:
    """Consume a live or replay async feed through the same agent pipeline."""
    agent = RavenAgent(on_tick=_render)
    n = 0
    async for frame in frames:
        agent.on_frame(frame)
        n += 1
    total_pnl = sum(result.realized_spread_pnl for result in agent.results)
    receipts = sum(1 for result in agent.results if result.receipt is not None)
    print("-" * 60)
    print(f"ticks={n}  spread opportunity={total_pnl:.4f}  receipts={receipts}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raven")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run a deterministic end-to-end smoke test (no network)",
    )
    args = parser.parse_args(argv)

    if args.smoke:
        print("RAVEN smoke test — full pipeline F1→F2→F3→F4→F6→F7")
        print("-" * 60)
        return run(_smoke_frames())

    settings = load_settings()
    print(f"RAVEN starting in {settings.feed_mode} mode")
    replay_name = Path(settings.replay_file).name
    if settings.is_replay and replay_name in {
        "scores_historical.jsonl",
        "scores_historical_18222446.jsonl",
    }:
        from raven.web.driver import iter_verified_frames

        print("Using merged real TxLINE score + odds historical replay")
        return run(iter_verified_frames())

    from raven.feed.source import build_source

    source = build_source(settings)
    return asyncio.run(run_async(source.frames()))


if __name__ == "__main__":
    sys.exit(main())
