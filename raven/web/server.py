"""Stdlib HTTP + SSE server for the RAVEN Web Control Room.

No third-party dependencies. Uses :mod:`http.server` with a threading mixin so
each browser tab gets its own replay stream over Server-Sent Events (SSE).

Routes
------
``GET /``            -> the single-page Control Room (``index.html``)
``GET /app.js``      -> frontend logic
``GET /styles.css``  -> frontend styling
``GET /raven.png``   -> the RAVEN logo
``GET /stream``      -> ``text/event-stream`` of serialized ticks (SSE)
``GET /healthz``     -> liveness probe

The ``/stream`` handler drives :func:`raven.web.driver.run_replay`, pushing one
``data:`` event per tick. Speed is controllable via ``?speed=`` (frames/sec).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from raven.web.driver import run_replay

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "RAVEN.png")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


@lru_cache(maxsize=1)
def _counterfactual_payload() -> bytes:
    from raven.counterfactual import run_counterfactual

    result = run_counterfactual(print_result=False)
    payload = result.to_dict()
    payload["anchored_proofs"] = 4
    payload["txline_onchain_proofs"] = 1
    payload["onchain_proofs"] = 5
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _content_type(path: str) -> str:
    _, ext = os.path.splitext(path)
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


class RavenHandler(BaseHTTPRequestHandler):
    server_version = "RAVEN-ControlRoom/1.0"

    # Quieter logging: one concise line per request.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def handle_one_request(self) -> None:
        # Browsers/curl frequently drop SSE connections mid-flight; swallow the
        # resulting reset so it doesn't spam the console with tracebacks.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


    def _send_headers(
        self, status: int, content_type: str, extra: Optional[dict] = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # Allow the Vercel-hosted frontend to consume this backend cross-origin.
        # EventSource never sends credentials, so a wildcard origin is safe here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # CORS preflight (browsers rarely preflight simple GETs, but be safe).
        self._send_headers(204, "text/plain; charset=utf-8")


    def _serve_static(self, filename: str) -> None:
        path = os.path.join(_STATIC_DIR, filename)
        if not os.path.isfile(path):
            self._send_headers(404, "text/plain; charset=utf-8")
            self.wfile.write(b"404 Not Found")
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self._send_headers(200, _content_type(path))
        self.wfile.write(body)

    def _serve_logo(self) -> None:
        path = os.path.abspath(_LOGO_PATH)
        if not os.path.isfile(path):
            self._send_headers(404, "text/plain; charset=utf-8")
            self.wfile.write(b"logo not found")
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self._send_headers(200, "image/png")
        self.wfile.write(body)

    def _serve_stream(self, speed: float) -> None:
        self._send_headers(
            200,
            "text/event-stream",
            extra={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        try:
            for tick in run_replay(speed=speed):
                payload = json.dumps(tick, ensure_ascii=False, separators=(",", ":"))
                chunk = f"data: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
            # Signal end-of-stream so the UI can show a summary.
            self.wfile.write(b"event: done\ndata: {}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Browser navigated away / closed the tab — expected.
            return
        except Exception as exc:  # pragma: no cover - defensive
            try:
                err = json.dumps({"error": str(exc)})
                self.wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if route == "/app.js":
            self._serve_static("app.js")
            return
        if route == "/config.js":
            self._serve_static("config.js")
            return

        if route == "/styles.css":
            self._serve_static("styles.css")
            return
        if route == "/control-room.css":
            self._serve_static("control-room.css")
            return
        if route in ("/raven.png", "/favicon.ico"):
            self._serve_logo()
            return
        if route == "/healthz":
            self._send_headers(200, "application/json")
            self.wfile.write(b'{"status":"ok"}')
            return
        if route == "/counterfactual":
            self._send_headers(
                200,
                "application/json",
                extra={"Cache-Control": "public, max-age=3600"},
            )
            self.wfile.write(_counterfactual_payload())
            return
        if route == "/stream":
            qs = parse_qs(parsed.query)
            try:
                speed = float(qs.get("speed", ["12"])[0])
            except (TypeError, ValueError):
                speed = 12.0
            self._serve_stream(speed)
            return

        self._send_headers(404, "text/plain; charset=utf-8")
        self.wfile.write(b"404 Not Found")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Start the Control Room server (blocking)."""
    httpd = ThreadingHTTPServer((host, port), RavenHandler)
    url = f"http://{host}:{port}"
    print("=" * 60)
    print("  RAVEN — Web Control Room")
    print("=" * 60)
    print(f"  Serving at   {url}")
    print("  Feed         real TxLINE replay · fixture 18222446")
    print(f"  Stream       {url}/stream")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down RAVEN Control Room…")
    finally:
        httpd.server_close()
