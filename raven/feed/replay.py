"""Recorder + ReplaySource for RAVEN's Verified Feed Layer (F1 / F8).

The hackathon has two hard realities:

1. Submissions **must** integrate real TxLINE data as a live input.
2. Matches will have ended by the time judges review, so there may be no live
   activity during evaluation.

RAVEN resolves this without ever fabricating data:

* :class:`Recorder` durably captures every *real* payload seen on the live SSE
  connection to a newline-delimited JSON (JSONL) file.
* :class:`ReplaySource` streams those *real captured* payloads back through the
  exact same normalizer, at a configurable speed, so judges watch RAVEN make
  live decisions on genuine World Cup data during review week.

There is no synthetic/mock generator anywhere in this module. Replay only ever
emits bytes that were actually received from TxLINE.

On-disk record format (one JSON object per line)::

    {"recv_ms": 1784412234981, "seq": 428, "payload": { ...raw TxLINE... }}

``recv_ms`` is RAVEN's wall-clock receive time, used only to reconstruct the
original inter-frame timing during replay. ``payload`` is the untouched TxLINE
object, which is what gets re-normalized and hashed — guaranteeing the replayed
``payload_hash`` matches the one produced live (determinism for F7/F8).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, TextIO

from .model import VerifiedFrame
from .normalize import normalize
from .source import FeedSource


def _timestamped_filename(prefix: str = "txline", ext: str = "jsonl") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{prefix}-{stamp}.{ext}"


class Recorder:
    """Append-only writer that persists raw TxLINE payloads to JSONL.

    Used by the live source to capture everything it sees. Safe to use as an
    async context manager. Also maintains a stable ``latest.jsonl`` pointer so
    the default replay config always finds the most recent capture.
    """

    def __init__(self, record_dir: str, filename: Optional[str] = None) -> None:
        self._dir = Path(record_dir)
        self._filename = filename or _timestamped_filename()
        self._path = self._dir / self._filename
        self._fh: Optional[TextIO] = None
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # Line-buffered so a crash mid-run still leaves a valid partial capture.
        self._fh = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, payload: Mapping[str, Any], *, seq: Optional[int] = None) -> None:
        """Persist a single raw payload with its receive timestamp."""
        if self._fh is None:
            self.open()
        record = {
            "recv_ms": int(time.time() * 1000),
            "seq": seq,
            "payload": payload,
        }
        assert self._fh is not None
        self._fh.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._count += 1

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except (OSError, ValueError):
                pass
            self._fh.close()
            self._fh = None
        self._update_latest_pointer()

    def _update_latest_pointer(self) -> None:
        """Point ``latest.jsonl`` at this capture (best-effort)."""
        if self._count == 0:
            return
        latest = self._dir / "latest.jsonl"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            # Relative symlink keeps the recordings dir portable.
            latest.symlink_to(self._filename)
        except (OSError, NotImplementedError):
            # Filesystems without symlink support: copy instead.
            try:
                latest.write_text(
                    self._path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass

    def __enter__(self) -> "Recorder":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class ReplaySource(FeedSource):
    """Replays real captured TxLINE payloads from a JSONL file.

    Timing between frames is reconstructed from the recorded ``recv_ms`` deltas
    and divided by ``speed`` (e.g. ``speed=50`` replays a match ~50x faster).
    Set ``speed<=0`` to replay as fast as possible (no sleeps) — useful for the
    deterministic counterfactual runs in F8 where wall-clock timing is irrelevant.
    """

    mode = "replay"

    def __init__(self, replay_file: str, speed: float = 50.0) -> None:
        self._path = Path(replay_file)
        self._speed = speed

    async def open(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(
                f"Replay file not found: {self._path}. Record a live session "
                f"first (RAVEN_FEED_MODE=live) or point RAVEN_REPLAY_FILE at a "
                f"valid capture."
            )

    def frames(self) -> AsyncIterator[VerifiedFrame]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[VerifiedFrame]:
        prev_recv_ms: Optional[int] = None
        fallback_seq = 0
        with open(self._path, "r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Skip a corrupt line rather than aborting the whole replay.
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    # TxLINE historical downloads are raw JSONL records while
                    # live captures use the Recorder envelope. Both represent
                    # genuine provider bytes and share the same normalizer.
                    payload = record

                recv_ms = record.get("recv_ms", payload.get("Ts"))
                await self._pace(prev_recv_ms, recv_ms)
                if isinstance(recv_ms, int):
                    prev_recv_ms = recv_ms

                fallback_seq += 1
                yield normalize(payload, fallback_sequence=fallback_seq)

    async def _pace(
        self, prev_recv_ms: Optional[int], recv_ms: Optional[Any]
    ) -> None:
        """Sleep to reproduce original inter-frame timing, scaled by speed."""
        if self._speed <= 0:
            return
        if prev_recv_ms is None or not isinstance(recv_ms, int):
            return
        delta_ms = max(0, recv_ms - prev_recv_ms)
        # Cap any pathological gap (e.g. half-time) so demos stay watchable.
        delta_ms = min(delta_ms, 30_000)
        await asyncio.sleep((delta_ms / 1000.0) / self._speed)
