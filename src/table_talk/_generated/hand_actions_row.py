# AUTO-GENERATED FROM schemas/hand_actions.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandActionsRow:
    hand_start_id: str
    hand_setup_id: str
    clip_id: str
    video_id: str
    hand_action_state: dict[str, Any]
    street_frame_gcs_paths: list[str]
