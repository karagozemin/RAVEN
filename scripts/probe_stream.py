#!/usr/bin/env python3
"""Quick probe: connect to stream, print first 10 raw SSE lines."""
import http.client, ssl, json, time, sys, urllib.request

BASE = "txline-dev.txodds.com"
API  = "txoracle_api_09d0b54ccd6b4964a1ef36aef3c5b340"

def fresh_jwt(host=BASE):
    req = urllib.request.Request(
        f"https://{host}/auth/guest/start",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]

jwt = fresh_jwt()
print(f"JWT ok (len={len(jwt)})")

ctx  = ssl.create_default_context()
conn = http.client.HTTPSConnection(BASE, context=ctx, timeout=30)
conn.request("GET", "/api/scores/stream", headers={
    "Authorization":  f"Bearer {jwt}",
    "X-Api-Token":    API,
    "Accept":         "text/event-stream",
    "Cache-Control":  "no-cache",
    "Accept-Encoding": "identity",
})
resp = conn.getresponse()
print(f"HTTP {resp.status}")
if resp.status != 200:
    print(resp.read(200))
    sys.exit(1)

t0  = time.time()
buf = b""
n   = 0
timeout = 20  # seconds

while time.time() - t0 < timeout:
    chunk = resp.read(256)
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        txt = line.decode("utf-8", errors="replace").strip()
        if txt:
            print(f"[{time.time()-t0:5.1f}s] {txt[:180]}")
            n += 1
            if n >= 15:
                conn.close()
                sys.exit(0)

print(f"\ntimeout after {time.time()-t0:.1f}s, got {n} lines")
conn.close()
