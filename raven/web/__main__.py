"""``python -m raven.web`` entrypoint for the Web Control Room."""

from __future__ import annotations

import argparse
import os

from raven.web.server import serve


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="raven.web",
        description="RAVEN Web Control Room — live decision dashboard over real "
        "TxLINE replay.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("RAVEN_WEB_HOST", "0.0.0.0"),
        help="Interface to bind (default: 0.0.0.0 for container hosts).",
    )
    parser.add_argument(
        "--port",
        type=int,
        # Render/Railway/Fly inject the port to bind via $PORT.
        default=int(os.environ.get("PORT", os.environ.get("RAVEN_WEB_PORT", "8787"))),
        help="Port to listen on (default: $PORT or 8787).",
    )

    args = parser.parse_args()
    serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
