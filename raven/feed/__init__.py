"""RAVEN Verified Feed Layer (F1).

Public surface for the feed layer. Downstream code imports from here rather
than reaching into submodules, keeping the ingestion contract stable.
"""

from __future__ import annotations

from .model import (
    FrameKind,
    MatchEventType,
    OddsSnapshot,
    Score,
    VerifiedFrame,
    canonical_hash,
)
from .normalize import normalize
from .source import FeedSource
from .live import LiveSSESource
from .replay import ReplaySource, Recorder

__all__ = [
    "FrameKind",
    "MatchEventType",
    "OddsSnapshot",
    "Score",
    "VerifiedFrame",
    "canonical_hash",
    "normalize",
    "FeedSource",
    "LiveSSESource",
    "ReplaySource",
    "Recorder",
    "build_source",
]


def build_source(settings) -> FeedSource:
    """Factory: pick the concrete FeedSource from runtime settings.

    There is no synthetic/mock path in the shipped product — only ``live``
    (real TxLINE SSE) and ``replay`` (real captured bytes replayed).
    """
    if settings.is_live:
        return LiveSSESource(
            url=settings.txline_sse_url,
            api_key=settings.txline_api_key,
            competition=settings.txline_competition,
            service_level=settings.txline_service_level,
            record_dir=settings.record_dir,
        )
    return ReplaySource(
        replay_file=settings.replay_file,
        speed=settings.replay_speed,
    )
