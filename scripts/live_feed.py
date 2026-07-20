#!/usr/bin/env python3
"""Start the production dual-stream TxLINE client in live mode."""

from __future__ import annotations

import os


def main() -> int:
    os.environ["RAVEN_FEED_MODE"] = "live"
    from raven.main import main as raven_main

    return raven_main([])


if __name__ == "__main__":
    raise SystemExit(main())
