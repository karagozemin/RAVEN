"""Configuration loader for RAVEN.

Reads settings from environment variables (optionally via a .env file) and
exposes a single typed ``Settings`` object. No secrets are hard-coded; the
canonical template lives in ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (avoids an external dependency).

    Lines of the form ``KEY=VALUE`` are injected into ``os.environ`` unless the
    key is already set. Comments (``#``) and blank lines are ignored.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    # Feed
    feed_mode: str  # "live" | "replay"

    # TxLINE (real endpoint)
    txline_sse_url: str
    txline_api_token: str
    txline_jwt: str
    txline_competition: str
    txline_service_level: int

    # Recording
    record_dir: str

    # Replay
    replay_file: str
    replay_speed: float

    # Solana
    solana_cluster: str
    solana_keypair_path: Optional[str]

    @property
    def is_live(self) -> bool:
        return self.feed_mode.lower() == "live"

    @property
    def is_replay(self) -> bool:
        return self.feed_mode.lower() == "replay"


def load_settings(dotenv_path: str = ".env") -> Settings:
    """Load settings from the environment (and an optional .env file)."""
    _load_dotenv(dotenv_path)
    return Settings(
        feed_mode=_get("RAVEN_FEED_MODE", "replay"),
        txline_sse_url=_get("TXLINE_SSE_URL", ""),
        txline_api_token=_get("TXLINE_API_TOKEN", _get("TXLINE_API_KEY", "")),
        txline_jwt=_get("TXLINE_JWT", ""),
        txline_competition=_get("TXLINE_COMPETITION", "worldcup"),
        txline_service_level=_get_int("TXLINE_SERVICE_LEVEL", 12),
        record_dir=_get("RAVEN_RECORD_DIR", "data/recordings"),
        replay_file=_get("RAVEN_REPLAY_FILE", "data/recordings/latest.jsonl"),
        replay_speed=_get_float("RAVEN_REPLAY_SPEED", 50.0),
        solana_cluster=_get("SOLANA_CLUSTER", "devnet"),
        solana_keypair_path=_get("SOLANA_KEYPAIR_PATH") or None,
    )
