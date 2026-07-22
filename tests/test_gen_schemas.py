import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gen_schemas import generate_dataclass

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"


def _write_schema(tmp_path: Path, name: str, columns: list[dict]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(columns))
    return path


def test_repeated_string_maps_to_list_str(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        "widgets",
        [{"name": "tags", "type": "STRING", "mode": "REPEATED"}],
    )
    output = generate_dataclass(schema_path)
    assert "tags: list[str]" in output
    assert "tags: list[str] | None" not in output


def test_repeated_field_has_no_default_and_precedes_nullable(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        "widgets",
        [
            {"name": "widget_id", "type": "STRING", "mode": "REQUIRED"},
            {"name": "tags", "type": "STRING", "mode": "REPEATED"},
            {"name": "note", "type": "STRING", "mode": "NULLABLE"},
        ],
    )
    output = generate_dataclass(schema_path)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    widget_id_idx = lines.index("widget_id: str")
    tags_idx = lines.index("tags: list[str]")
    note_idx = lines.index("note: str | None = None")
    assert widget_id_idx < tags_idx < note_idx


def test_hand_starts_schema_generates_expected_dataclass():
    output = generate_dataclass(SCHEMAS_DIR / "hand_starts.json")
    assert "class HandStartsRow:" in output
    assert "from typing import Any" in output
    assert "    hand_start_state: dict[str, Any]" in output
    assert "    verify_frame_gcs_paths: list[str]" in output
    assert "detected_at" not in output


def test_hand_setup_processing_attempts_schema_generates_expected_dataclass():
    output = generate_dataclass(SCHEMAS_DIR / "hand_setup_processing_attempts.json")
    assert "class HandSetupProcessingAttemptsRow:" in output
    assert "    attempt_id: str" in output
    assert "    hand_setup_id: str" in output
    assert "    status: str" in output
    assert "    status_message: str | None = None" in output
    assert "attempted_at" not in output
