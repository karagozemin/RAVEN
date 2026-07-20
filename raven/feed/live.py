"""Authenticated live TxLINE odds + scores SSE ingestion."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import AsyncIterator, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from .model import VerifiedFrame
from .normalize import normalize
from .replay import Recorder
from .source import FeedSource


class LiveSSESource(FeedSource):
    """Merge the official TxLINE odds and scores streams into one ordered feed.

    TxLINE requires a short-lived guest JWT in ``Authorization`` and the
    activated subscription token in ``X-Api-Token``. A 401 refreshes the guest
    JWT automatically; transport failures reconnect with bounded backoff.
    Every accepted payload is recorded before normalization.
    """

    mode = "live"

    def __init__(
        self,
        url: str,
        api_token: str,
        guest_jwt: str = "",
        competition: str = "worldcup",
        service_level: int = 12,
        record_dir: str = "data/recordings",
    ) -> None:
        self._api_base = self._normalise_api_base(url)
        self._api_token = api_token
        self._jwt = guest_jwt
        self._competition = competition
        self._service_level = service_level
        self._recorder = Recorder(record_dir)
        self._client: Optional["httpx.AsyncClient"] = None
        self._jwt_lock = asyncio.Lock()
        self._max_backoff_s = 30.0

    @staticmethod
    def _normalise_api_base(url: str) -> str:
        value = (url or "https://txline-dev.txodds.com/api").rstrip("/")
        for suffix in ("/odds/stream", "/scores/stream"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
        return value

    @property
    def stream_urls(self) -> tuple[str, str]:
        return (f"{self._api_base}/odds/stream", f"{self._api_base}/scores/stream")

    @property
    def auth_origin(self) -> str:
        parsed = urlsplit(self._api_base)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    async def open(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for live TxLINE ingestion")
        if not self._api_token:
            raise RuntimeError("TXLINE_API_TOKEN is required for live TxLINE ingestion")
        self._recorder.open()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        if not self._jwt:
            await self._refresh_jwt()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._recorder.close()

    async def _refresh_jwt(self) -> str:
        async with self._jwt_lock:
            assert self._client is not None
            response = await self._client.post(
                f"{self.auth_origin}/auth/guest/start",
                json={},
                timeout=30.0,
            )
            response.raise_for_status()
            token = response.json().get("token")
            if not token:
                raise RuntimeError("TxLINE guest authentication returned no token")
            self._jwt = str(token)
            return self._jwt

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt}",
            "X-Api-Token": self._api_token,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

    def frames(self) -> AsyncIterator[VerifiedFrame]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[VerifiedFrame]:
        if self._client is None:
            await self.open()
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=2048)
        tasks = [
            asyncio.create_task(self._consume_stream(url, queue))
            for url in self.stream_urls
        ]
        fallback_sequence = 0
        try:
            while True:
                payload = await queue.get()
                fallback_sequence += 1
                native_sequence = payload.get("Seq") or payload.get("sequence") or payload.get("seq")
                self._recorder.write(payload, seq=native_sequence)
                frame = normalize(payload, fallback_sequence=fallback_sequence)
                yield replace(frame, sequence=fallback_sequence)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close()

    async def _consume_stream(self, url: str, queue: asyncio.Queue[dict]) -> None:
        assert self._client is not None
        backoff = 1.0
        while True:
            try:
                async with self._client.stream("GET", url, headers=self._headers()) as response:
                    if response.status_code == 401:
                        await self._refresh_jwt()
                        continue
                    response.raise_for_status()
                    backoff = 1.0
                    async for payload in self._read_events(response):
                        await queue.put(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._max_backoff_s)

    @staticmethod
    async def _read_events(response: "httpx.Response") -> AsyncIterator[dict]:
        data_lines: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line:
                if data_lines:
                    payload = LiveSSESource._decode(data_lines)
                    data_lines = []
                    if isinstance(payload, dict):
                        yield payload
                    elif isinstance(payload, list):
                        for item in payload:
                            if isinstance(item, dict):
                                yield item
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if field == "data":
                data_lines.append(value.lstrip(" ") if separator else "")

    @staticmethod
    def _decode(data_lines: list[str]):
        import json

        try:
            return json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return None
