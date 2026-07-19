"""Live TxLINE SSE source for RAVEN's Verified Feed Layer (F1).

This is the *only* path that touches the network. It opens a Server-Sent
Events (SSE) connection to the real TxLINE World Cup endpoint, parses each
event, records the untouched payload to disk (so it can be replayed verbatim
during judging), and yields normalized :class:`VerifiedFrame` instances.

Design notes:

* **No mock data, ever.** This source only emits bytes that actually arrived
  from TxLINE. The recording it produces is what feeds :class:`ReplaySource`
  later, guaranteeing the replayed ``payload_hash`` matches the live one.
* **Record-on-ingest.** Every raw payload is persisted *before* it is yielded,
  so a crash mid-run still leaves a faithful, replayable capture.
* **Resilient.** Transient network drops trigger a bounded exponential backoff
  reconnect rather than killing the agent.

SSE wire format handled (per the W3C EventSource spec)::

    id: 428
    event: odds
    data: {"fixture_id": 17952170, "odds": {...}, "sequence": 428}
    <blank line terminates the event>

``data:`` may span multiple lines; they are concatenated with newlines before
JSON parsing.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Dict, Optional

try:  # httpx is the async HTTP client used for the streaming connection.
    import httpx
except ImportError:  # pragma: no cover - surfaced with a clear message at run
    httpx = None  # type: ignore[assignment]

from .model import VerifiedFrame
from .normalize import normalize
from .replay import Recorder
from .source import FeedSource


class LiveSSESource(FeedSource):
    """Streams real TxLINE World Cup data over SSE and records every frame.

    Parameters
    ----------
    url:
        Base TxLINE SSE endpoint (real, from the hackathon docs).
    api_key:
        TxLINE API key. Sent as a Bearer token; never logged.
    competition:
        Competition slug (e.g. ``"worldcup"``).
    service_level:
        TxLINE service level (``12`` = real-time, ``1`` = 60s delayed).
    record_dir:
        Directory where raw payloads are captured as JSONL for later replay.
    """

    mode = "live"

    def __init__(
        self,
        url: str,
        api_key: str,
        competition: str = "worldcup",
        service_level: int = 12,
        record_dir: str = "data/recordings",
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._competition = competition
        self._service_level = service_level
        self._recorder = Recorder(record_dir)
        self._client: Optional["httpx.AsyncClient"] = None

        # Reconnect policy.
        self._max_backoff_s = 30.0
        self._base_backoff_s = 1.0

    # -- resource lifecycle ------------------------------------------------

    async def open(self) -> None:
        if httpx is None:
            raise RuntimeError(
                "httpx is required for the live TxLINE source. Install it with "
                "`pip install httpx` (see requirements.txt), or run in replay "
                "mode (RAVEN_FEED_MODE=replay)."
            )
        if not self._url:
            raise RuntimeError(
                "TXLINE_SSE_URL is empty. Set the real TxLINE endpoint in .env "
                "before running in live mode."
            )
        self._recorder.open()
        # No fixed timeout on the read: SSE is a long-lived stream.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._recorder.close()

    # -- request shaping ---------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _params(self) -> Dict[str, str]:
        return {
            "competition": self._competition,
            "serviceLevel": str(self._service_level),
        }

    # -- frame production --------------------------------------------------

    def frames(self) -> AsyncIterator[VerifiedFrame]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[VerifiedFrame]:
        if self._client is None:
            await self.open()
        assert self._client is not None

        fallback_seq = 0
        backoff = self._base_backoff_s

        while True:
            try:
                async with self._client.stream(
                    "GET",
                    self._url,
                    headers=self._headers(),
                    params=self._params(),
                ) as response:
                    response.raise_for_status()
                    backoff = self._base_backoff_s  # reset on a clean connect

                    async for payload in self._read_events(response):
                        fallback_seq += 1
                        native_seq = payload.get("sequence") or payload.get("seq")
                        self._recorder.write(payload, seq=native_seq)
                        yield normalize(payload, fallback_sequence=fallback_seq)

            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on any transport error
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._max_backoff_s)
                continue

    async def _read_events(
        self, response: "httpx.Response"
    ) -> AsyncIterator[dict]:
        """Parse an SSE byte stream into raw TxLINE payload dicts.

        Accumulates ``data:`` lines until a blank line delimits the event,
        then JSON-decodes the concatenated data. Malformed events are skipped
        rather than aborting the stream.
        """
        data_lines: list[str] = []

        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")

            if line == "":
                # Blank line: dispatch the accumulated event, if any.
                if data_lines:
                    payload = self._decode(data_lines)
                    data_lines = []
                    if payload is not None:
                        yield payload
                continue

            if line.startswith(":"):
                # SSE comment / keep-alive heartbeat.
                continue

            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value

            if field == "data":
                data_lines.append(value)
            # ``id`` / ``event`` fields are informational here; the payload
            # itself carries the authoritative sequence/type for RAVEN.

        # Flush a trailing event if the stream ends without a final blank line.
        if data_lines:
            payload = self._decode(data_lines)
            if payload is not None:
                yield payload

    @staticmethod
    def _decode(data_lines: list[str]) -> Optional[dict]:
        text = "\n".join(data_lines).strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
