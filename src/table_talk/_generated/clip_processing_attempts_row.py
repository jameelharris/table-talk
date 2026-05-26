# AUTO-GENERATED FROM schemas/clip_processing_attempts.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClipProcessingAttemptsRow:
    clip_id: str
    status: str
    status_message: str | None = None
