#!/usr/bin/env python3
"""Background recorder for the World Cup Final SSE stream.

Connects to TxLINE /api/scores/stream + /api/odds/stream using the JWT
from .env, records every raw frame to data/replay/final_live.jsonl, and
prints a one-liner per received frame so you can confirm real data is arriving.

Runs until killed (Ctrl-C) or until --timeout seconds elapsed.
"""
import asyncio
import json
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: .venv/bin/pip install httpx")
    sys.exit(1)

# ── load credentials from .env (already in environment or parse manually) ─
def _load_env() -> dict:
    env: dict = {}
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    # override with real os.environ
    for k in ("TXLINE_JWT", "TXLINE_API_TOKEN", "TXLINE_SSE_URL"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env

ENV = _load_env()
BASE_URL  = ENV.get("TXLINE_SSE_URL", "https://txline-dev.txodds.com/api")
JWT       = ENV.get("TXLINE_JWT", "")
API_TOKEN = ENV.get("TXLINE_API_TOKEN", "")

OUT_PATH  = os.path.join(os.path.dirname(__file__), "..", "data", "replay", "final_live.jsonl")

STREAMS = [
    ("scores", f"{BASE_URL}/scores/stream"),
    ("odds",   f"{BASE_URL}/odds/stream"),
]

# ── SSE parser ─────────────────────────────────────────────────────────────

async def _read_sse(client: httpx.AsyncClient, label: str, url: str,
                    out: "asyncio.Queue[dict]", stop: asyncio.Event) -> None:
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "Authorization": f"Bearer {JWT}",
    }
    params = {
        "competition": "worldcup",
        "serviceLevel": "1",
    }
    backoff = 1.0
    while not stop.is_set():
        try:
            async with client.stream("GET", url, headers=headers,
                                     params=params,
                                     timeout=httpx.Timeout(30.0, read=None)) as resp:
                resp.raise_for_status()
                backoff = 1.0
                data_lines: list[str] = []
                async for raw in resp.aiter_lines():
                    if stop.is_set():
                        break
                    line = raw.rstrip("\r")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines).strip()
                            data_lines = []
                            if text:
                                try:
                                    payload = json.loads(text)
                                    if isinstance(payload, dict):
                                        payload["_stream"] = label
                                        await out.put(payload)
                                except json.JSONDecodeError:
                                    pass
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    if field == "data":
                        data_lines.append(value.lstrip(" "))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"  [{label}] connection error: {exc} — retry in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def main(timeout_s: int) -> int:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    out_path = os.path.abspath(OUT_PATH)

    print("RAVEN Live Recorder — World Cup Final")
    print(f"  Base URL  : {BASE_URL}")
    print("  Auth      : configured")
    print(f"  Output    : {out_path}")
    print(f"  Timeout   : {timeout_s}s  (Ctrl-C to stop early)")
    print("-" * 60)

    queue: asyncio.Queue[dict] = asyncio.Queue()
    stop = asyncio.Event()

    async with httpx.AsyncClient() as client:
        tasks = [
            asyncio.create_task(_read_sse(client, lbl, url, queue, stop))
            for lbl, url in STREAMS
        ]

        deadline = time.monotonic() + timeout_s
        frame_count = 0
        with open(out_path, "a", encoding="utf-8") as fout:
            try:
                while time.monotonic() < deadline and not stop.is_set():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        remaining = int(deadline - time.monotonic())
                        print(f"  waiting... ({remaining}s remaining, "
                              f"{frame_count} frames so far)", flush=True)
                        continue

                    stream = payload.get("_stream", "?")
                    seq    = payload.get("Seq") or payload.get("sequence") or payload.get("seq") or "?"
                    action = payload.get("Action") or payload.get("event") or payload.get("type") or "?"
                    fix_id = payload.get("FixtureId") or payload.get("fixture_id") or "?"
                    score_raw = payload.get("Score") or {}
                    p1g = (score_raw.get("Participant1") or {})
                    p1g = (p1g.get("Total") or {}).get("Goals", "?")
                    p2g = (score_raw.get("Participant2") or {})
                    p2g = (p2g.get("Total") or {}).get("Goals", "?")

                    frame_count += 1
                    fout.write(json.dumps(payload) + "\n")
                    fout.flush()

                    # Mark shocks
                    shock_mark = " *** SHOCK ***" if action in {"goal", "red_card", "var"} else ""
                    print(f"  [{stream:6s}] seq={seq:<6} action={str(action):<22} "
                          f"fix={fix_id}  score={p1g}-{p2g}{shock_mark}", flush=True)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
            finally:
                stop.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    print("-" * 60)
    print(f"Recorded {frame_count} frames → {out_path}")
    if frame_count == 0:
        print("WARNING: zero frames received. Stream may require higher service level or different auth.")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=90,
                        help="Seconds to record before stopping (default 90)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.timeout)))
