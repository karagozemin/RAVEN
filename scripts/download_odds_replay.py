#!/usr/bin/env python3
"""Download real TxLINE consensus odds for the packaged replay fixture.

The script uses the documented historical five-minute interval endpoint and
stores only full-match 1X2, Asian Handicap, and Over/Under updates for one
fixture. Credentials stay in ``.env`` and are never written to the replay.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


CORE_MARKETS = {
    "1X2_PARTICIPANT_RESULT",
    "ASIANHANDICAP_PARTICIPANT_GOALS",
    "OVERUNDER_PARTICIPANT_GOALS",
}

TARGET_PARAMETERS = {
    "ASIANHANDICAP_PARTICIPANT_GOALS": "line=-0.5",
    "OVERUNDER_PARTICIPANT_GOALS": "line=2.5",
}


def _request_json(url: str, headers: dict[str, str], *, data: bytes | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def _fresh_jwt(origin: str) -> str:
    payload = _request_json(
        f"{origin}/auth/guest/start",
        {"Content-Type": "application/json"},
        data=b"{}",
    )
    return str(payload["token"])


def _intervals(start_ms: int, end_ms: int) -> list[tuple[int, int, int]]:
    cursor = (start_ms // 300_000) * 300_000
    end = ((end_ms // 300_000) + 1) * 300_000
    result: list[tuple[int, int, int]] = []
    while cursor <= end:
        dt = datetime.fromtimestamp(cursor / 1000, timezone.utc)
        result.append((cursor // 86_400_000, dt.hour, dt.minute // 5))
        cursor += 300_000
    return result


def _eligible(record: Any, fixture_id: int) -> bool:
    if not isinstance(record, dict):
        return False
    if int(record.get("FixtureId", 0)) != fixture_id:
        return False
    if record.get("SuperOddsType") not in CORE_MARKETS:
        return False
    expected_parameters = TARGET_PARAMETERS.get(str(record.get("SuperOddsType")))
    if expected_parameters is not None and record.get("MarketParameters") != expected_parameters:
        return False
    if record.get("MarketPeriod") not in (None, ""):
        return False
    prices = record.get("Prices")
    names = record.get("PriceNames")
    return isinstance(prices, list) and isinstance(names, list) and len(prices) == len(names) and bool(prices)


def download(fixture_id: int, scores_path: Path, output_path: Path, workers: int = 6) -> int:
    load_dotenv()
    api_base = os.environ.get("TXLINE_SSE_URL", "https://txline-dev.txodds.com/api").rstrip("/")
    api_token = os.environ.get("TXLINE_API_TOKEN", "")
    if not api_token:
        raise RuntimeError("TXLINE_API_TOKEN is required")
    origin = api_base[:-4] if api_base.endswith("/api") else api_base
    jwt = _fresh_jwt(origin)
    headers = {
        "Authorization": f"Bearer {jwt}",
        "X-Api-Token": api_token,
        "Accept": "application/json",
    }

    score_records = [
        json.loads(line)
        for line in scores_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    timestamps = [int(row["Ts"]) for row in score_records if row.get("Ts")]
    if not timestamps:
        raise ValueError(f"No TxLINE timestamps in {scores_path}")
    fixture_start = min(
        [int(row["StartTime"]) for row in score_records if row.get("StartTime")]
        or timestamps
    )
    start_ms = min(max(min(timestamps), fixture_start - 30 * 60_000), fixture_start)
    end_ms = max(timestamps)
    intervals = _intervals(start_ms, end_ms)

    def fetch(slot: tuple[int, int, int]) -> list[dict[str, Any]]:
        day, hour, interval = slot
        url = f"{api_base}/odds/updates/{day}/{hour}/{interval}"
        payload = _request_json(url, headers)
        rows = payload if isinstance(payload, list) else [payload]
        return [row for row in rows if _eligible(row, fixture_id)]

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch, slot) for slot in intervals]
        for future in as_completed(futures):
            records.extend(future.result())

    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        key = json.dumps(record, sort_keys=True, separators=(",", ":"))
        unique[key] = record
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            int(row.get("Ts", 0)),
            str(row.get("SuperOddsType", "")),
            str(row.get("MarketParameters", "")),
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in ordered:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    counts = {market: 0 for market in CORE_MARKETS}
    for record in ordered:
        counts[str(record["SuperOddsType"])] += 1
    print(f"Saved {len(ordered)} real TxLINE odds updates to {output_path}")
    for market in sorted(counts):
        print(f"  {market}: {counts[market]}")
    return len(ordered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=int, default=18222446)
    parser.add_argument(
        "--scores",
        type=Path,
        default=Path("data/replay/scores_historical_18222446.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/replay/odds_historical_18222446.jsonl"),
    )
    args = parser.parse_args()
    return 0 if download(args.fixture, args.scores, args.output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
