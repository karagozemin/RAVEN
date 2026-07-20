#!/usr/bin/env python3
"""Stream the packaged real replay through the production web driver."""

from __future__ import annotations

import argparse

from raven.web.driver import run_replay


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=12.0, help="frames per second")
    args = parser.parse_args()

    for tick in run_replay(speed=args.speed):
        if tick["transitioned"] or tick["receipt"]:
            receipt = tick["receipt"]
            proof = " anchored" if receipt and receipt["anchored"] else ""
            print(
                f"#{tick['sequence']:<4} {tick['state']:<12} "
                f"{tick['reason']}{proof}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
