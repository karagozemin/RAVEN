"""RAVEN Web Control Room.

A zero-dependency (stdlib-only) web dashboard that streams RAVEN's live
decisions to the browser over Server-Sent Events (SSE) while the agent replays
the real captured TxLINE feed.

Run it with::

    python -m raven.web

then open http://127.0.0.1:8787 in a browser.
"""

from raven.web.server import serve

__all__ = ["serve"]
