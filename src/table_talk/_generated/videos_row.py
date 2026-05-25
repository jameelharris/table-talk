# AUTO-GENERATED FROM schemas/videos.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideosRow:
    video_id: str
    source_url: str
    title: str
    duration_seconds: int
    gcs_path: str
    file_size_bytes: int
