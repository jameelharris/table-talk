#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"
OUTPUT_DIR = REPO_ROOT / "src" / "table_talk" / "_generated"

TYPE_MAP = {
    "STRING": "str",
    "INT64": "int",
    "FLOAT64": "float",
    "BOOL": "bool",
    "BYTES": "bytes",
}


def schema_name_to_class(stem: str) -> str:
    return "".join(word.title() for word in stem.split("_")) + "Row"


def generate_dataclass(schema_path: Path) -> str:
    stem = schema_path.stem
    class_name = schema_name_to_class(stem)
    columns = json.loads(schema_path.read_text())

    required_fields: list[tuple[str, str]] = []
    nullable_fields: list[tuple[str, str]] = []

    for col in columns:
        if "defaultValueExpression" in col:
            continue

        name = col["name"]
        bq_type = col["type"]
        mode = col["mode"]

        if mode == "REPEATED":
            print(
                f"Error: {schema_path.name}: column '{name}' has mode REPEATED,"
                " which is not supported.",
                file=sys.stderr,
            )
            sys.exit(1)

        if bq_type not in TYPE_MAP:
            print(
                f"Error: {schema_path.name}: column '{name}' has unsupported type '{bq_type}'.",
                file=sys.stderr,
            )
            sys.exit(1)

        py_type = TYPE_MAP[bq_type]

        if mode == "REQUIRED":
            required_fields.append((name, py_type))
        elif mode == "NULLABLE":
            nullable_fields.append((name, py_type))

    lines = [
        f"# AUTO-GENERATED FROM schemas/{schema_path.name} BY scripts/gen_schemas.py",
        "# DO NOT EDIT BY HAND. Run `uv run python scripts/gen_schemas.py` to regenerate.",
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
        "@dataclass(frozen=True)",
        f"class {class_name}:",
    ]

    for name, py_type in required_fields:
        lines.append(f"    {name}: {py_type}")
    for name, py_type in nullable_fields:
        lines.append(f"    {name}: {py_type} | None = None")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for schema_path in sorted(SCHEMAS_DIR.glob("*.json")):
        content = generate_dataclass(schema_path)
        output_file = OUTPUT_DIR / (schema_path.stem + "_row.py")
        output_file.write_text(content)
        print(f"Generated {output_file.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
