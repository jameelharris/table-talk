# AUTO-GENERATED FROM schemas/video_ingestion_attempts.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoIngestionAttemptsRow:
    source_url: str
    status: str
    video_id: str | None = None
    status_message: str | None = None
    duration_ms: int | None = None
