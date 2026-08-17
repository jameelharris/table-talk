# AUTO-GENERATED FROM schemas/hand_start_processing_attempts.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HandStartProcessingAttemptsRow:
    attempt_id: str
    hand_start_id: str
    status: str
    status_message: str | None = None
