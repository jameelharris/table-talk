# AUTO-GENERATED FROM schemas/hand_starts.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandStartsRow:
    hand_start_id: str
    hand_setup_id: str
    clip_id: str
    video_id: str
    fva_time_seconds: int
    second_action_time_seconds: int
    hand_start_state: dict[str, Any]
    fva_frame_gcs_path: str
    verify_frame_gcs_paths: list[str]
