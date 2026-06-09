# AUTO-GENERATED FROM schemas/hand_setups.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandSetupsRow:
    hand_setup_id: str
    clip_id: str
    video_id: str
    hand_setup_time_seconds: int
    frame_gcs_path: str
    hand_setup_state: dict[str, Any]
