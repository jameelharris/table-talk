# AUTO-GENERATED FROM schemas/clip_manifest.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClipManifestRow:
    clip_id: str
    video_id: str
    clip_start_time: int
    clip_end_time: int
