"""Constraints BigQuery enforces on schemas/*.json that nothing else checks.

These files have two consumers — scripts/gen_schemas.py and Terraform — and
neither validates against BigQuery's own limits. gen_schemas.py reads name,
type, mode and defaultValueExpression and ignores descriptions entirely, so a
description that BigQuery will reject produces a clean codegen run, a green test
suite, and a failure at `terraform apply` with the corpus already half migrated.

That happened: the hand_action_state description grew past 1,024 characters when
extraction_status was documented inline, and three of four tables had applied
before the fourth was rejected.
"""

import json
from pathlib import Path

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"

# https://cloud.google.com/bigquery/quotas — a column description is capped at
# 1,024 characters. BigQuery rejects the table update outright rather than
# truncating.
MAX_DESCRIPTION_CHARS = 1024

SCHEMA_FILES = sorted(SCHEMAS_DIR.glob("*.json"))


def _columns(path: Path):
    return json.loads(path.read_text())


def test_schema_files_are_discovered():
    """A glob that silently matches nothing would make every test below vacuous."""
    assert SCHEMA_FILES, f"no schema files found under {SCHEMAS_DIR}"
    assert len(SCHEMA_FILES) >= 12


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.stem)
def test_every_description_is_within_bigquery_limit(path):
    over = [
        (col["name"], len(col.get("description", "")))
        for col in _columns(path)
        if len(col.get("description", "")) > MAX_DESCRIPTION_CHARS
    ]
    assert not over, (
        f"{path.name}: description(s) exceed BigQuery's "
        f"{MAX_DESCRIPTION_CHARS}-character column limit: {over}. "
        f"Terraform will reject the table update. Move the detail into "
        f"ARCHITECTURE.md and point at it from here."
    )


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.stem)
def test_every_column_has_a_description(path):
    """Descriptions are how the blob structure is documented for anyone reading
    the table rather than the repo."""
    missing = [
        col["name"] for col in _columns(path) if not col.get("description", "").strip()
    ]
    assert not missing, f"{path.name}: columns with no description: {missing}"
