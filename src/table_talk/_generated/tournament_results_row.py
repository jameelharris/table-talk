# AUTO-GENERATED FROM schemas/tournament_results.json BY scripts/gen_schemas.py
# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TournamentResultsRow:
    video_id: str
    bounty_type: str
    currency_symbol: str
    frame_timestamp_seconds: int
    frame_gcs_path: str
    tournament_results_state: dict[str, Any]
