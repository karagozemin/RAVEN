#!/usr/bin/env python3
"""
live_feed.py — RAVEN real-time TxLINE SSE feed consumer.

Connects to txline-dev.txodds.com/api/scores/stream, auto-renews
the guest JWT on 401, feeds every frame through the full RAVEN
pipeline (normalize → agent → provenance).

Usage:
    .venv/bin/python3 scripts/live_feed.py [--no-anchor] [--duration N]

    --no-anchor   skip Solana anchoring (noop mode)
    --duration N  stop after N seconds (default: run forever)
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dotenv
dotenv.load_dotenv()

from raven.feed.normalize import normalize
from raven.agent import RavenAgent
from raven.provenance.anchor import AnchorResult
from raven.provenance.store import ReceiptEmitter, ReceiptStore

BASE_URL   = os.environ.get("TXLINE_SSE_URL", "https://txline-dev.txodds.com/api")
API_TOKEN  = os.environ.get("TXLINE_API_TOKEN", "")
GUEST_URL  = BASE_URL.replace("/api", "") + "/auth/guest/start"
STREAM_URL = BASE_URL + "/scores/stream"

# ── Auth ──────────────────────────────────────────────────────────────────────
def fresh_jwt() -> str:
    req = urllib.request.Request(
        GUEST_URL,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    jwt = body["token"]
    print(f"  🔑 fresh JWT (ip={_jwt_ip(jwt)})", flush=True)
    return jwt

def _jwt_ip(jwt: str) -> str:
    import base64
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("maybeClientIp", "?")

# ── SSE parser ────────────────────────────────────────────────────────────────
def _sse_lines(response):
    """Yield raw SSE data strings from an http.client.HTTPResponse."""
    buf = b""
    while True:
        chunk = response.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                line = line.decode("utf-8", errors="replace")
                if line.startswith("data:"):
                    yield line[5:].strip()

# ── Anchors ───────────────────────────────────────────────────────────────────
class NoopAnchor:
    backend = "noop"
    def anchor(self, receipt):
        return AnchorResult(hash=receipt.commitment(), signature=None,
                            anchored=False, backend="noop")

def make_anchor(no_anchor: bool):
    if no_anchor:
        return NoopAnchor()
    try:
        from raven.provenance.anchor import SolanaAnchor
        a = SolanaAnchor(
            rpc_url="https://api.devnet.solana.com",
            keypair_path=os.environ.get("SOLANA_KEYPAIR_PATH", "_keys/raven-wallet.json"),
            commitment="confirmed",
        )
        print("⚡ Solana devnet anchor: ACTIVE", flush=True)
        return a
    except Exception as e:
        print(f"⚠️  Solana unavailable ({e}), noop mode", flush=True)
        return NoopAnchor()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-anchor", action="store_true")
    p.add_argument("--duration", type=float, default=0,
                   help="stop after N seconds (0=forever)")
    args = p.parse_args()

    anchor  = make_anchor(args.no_anchor)
    store   = ReceiptStore()
    emitter = ReceiptEmitter(anchor=anchor, store=store, on_emit=lambda r: None)
    agent   = RavenAgent(emitter=emitter)

    frames = shocks = receipts = 0
    t0 = time.time()
    jwt = fresh_jwt()

    print()
    print("═" * 68)
    print("  RAVEN LIVE FEED — real-time TxLINE SSE")
    print(f"  stream={STREAM_URL}")
    print("═" * 68)
    print()

    while True:
        if args.duration and (time.time() - t0) >= args.duration:
            break

        import http.client, ssl
        host = "txline-dev.txodds.com"
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, context=ctx, timeout=30)
        conn.request(
            "GET", "/api/scores/stream",
            headers={
                "Authorization":  f"Bearer {jwt}",
                "X-Api-Token":    API_TOKEN,
                "Accept":         "text/event-stream",
                "Cache-Control":  "no-cache",
                "Accept-Encoding": "identity",
            },
        )
        resp = conn.getresponse()

        if resp.status == 401:
            print("  ↩ 401 — renewing JWT…", flush=True)
            jwt = fresh_jwt()
            conn.close()
            continue
        if resp.status == 403:
            print(f"  ✗ 403 — token/network mismatch. API_TOKEN={API_TOKEN[:16]}…")
            sys.exit(1)
        if resp.status != 200:
            print(f"  ✗ unexpected status {resp.status}")
            sys.exit(1)

        print(f"  ✓ connected (HTTP 200), consuming SSE…", flush=True)
        try:
            for data_str in _sse_lines(resp):
                if not data_str:
                    continue
                try:
                    raw = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                frames += 1
                frame  = normalize(raw, fallback_sequence=frames)
                result = agent.on_frame(frame)

                if frame.is_shock:
                    shocks += 1
                    print(f"  ⚡ SHOCK seq#{frame.sequence} fixture={raw.get('FixtureId','?')} "
                          f"state={raw.get('GameState','?')}", flush=True)

                if result.receipt is not None:
                    receipts += 1
                    marker = "⚡" if getattr(result.receipt, "anchored", False) else "·"
                    print(f"  {marker} {result.state.value:<14} seq#{frame.sequence:<6} "
                          f"{(result.risk.reason[:50] if result.risk else '')}", flush=True)

                if args.duration and (time.time() - t0) >= args.duration:
                    break
        except Exception as e:
            print(f"  stream error: {e} — reconnecting…", flush=True)
            time.sleep(1)
        finally:
            conn.close()

    print()
    print("─" * 68)
    print(f"  frames={frames}  shocks={shocks}  receipts={receipts}"
          f"  elapsed={time.time()-t0:.1f}s")
    print("─" * 68)

if __name__ == "__main__":
    main()
