from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb

_DEFAULT_FIXTURE = Path(__file__).parent.parent / "fixtures" / "synthetic_hands.json"


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict[str, Any]]
    schema: dict[str, str]      # column name → type name
    bytes_processed: int
    execution_ms: int


@dataclass(frozen=True)
class DryRunResult:
    estimated_bytes: int
    valid: bool
    error: str | None


class BigQueryClient(Protocol):
    def execute_query(self, sql: str, max_rows: int = 100_000) -> QueryResult: ...
    def dry_run_query(self, sql: str) -> DryRunResult: ...
    def get_table_schema(self, table_name: str) -> dict[str, str]: ...


class RealBigQueryClient:
    # google-cloud-bigquery is already a project dependency.
    # Full implementation deferred until real BQ integration (post-hello-world).

    def execute_query(self, sql: str, max_rows: int = 100_000) -> QueryResult:
        raise NotImplementedError("Real BigQuery integration is post-hello-world")

    def dry_run_query(self, sql: str) -> DryRunResult:
        raise NotImplementedError("Real BigQuery integration is post-hello-world")

    def get_table_schema(self, table_name: str) -> dict[str, str]:
        raise NotImplementedError("Real BigQuery integration is post-hello-world")


class FakeBigQueryClient:
    """
    DuckDB-backed fake. Loads JSON fixture file on init; each top-level key
    becomes a table.

    SQL dialect note: DuckDB SQL is similar to BigQuery but not identical.
    Agents writing SQL for tests must stick to portable ANSI SQL — avoid
    BigQuery-specific functions (APPROX_QUANTILES, SAFE_DIVIDE, etc.) and
    non-standard UNNEST syntax. Standard GROUP BY, JOIN, CASE, window functions,
    and CTEs work fine.
    """

    def __init__(self, fixture_path: Path = _DEFAULT_FIXTURE) -> None:
        self._conn = duckdb.connect()
        with open(fixture_path) as f:
            data: dict[str, list[dict[str, Any]]] = json.load(f)
        for table_name, rows in data.items():
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                json.dump(rows, tmp)
                tmp_path = tmp.name
            try:
                self._conn.execute(
                    f"CREATE TABLE {table_name} AS SELECT * FROM read_json_auto(?)",
                    [tmp_path],
                )
            finally:
                Path(tmp_path).unlink()

    def execute_query(self, sql: str, max_rows: int = 100_000) -> QueryResult:
        start = time.monotonic()
        result = self._conn.execute(sql)
        rows_raw = result.fetchmany(max_rows)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        columns = [desc[0] for desc in result.description]
        schema = {desc[0]: str(desc[1]) for desc in result.description}
        rows = [dict(zip(columns, row)) for row in rows_raw]
        return QueryResult(
            rows=rows,
            schema=schema,
            bytes_processed=len(json.dumps(rows)),
            execution_ms=elapsed_ms,
        )

    def dry_run_query(self, sql: str) -> DryRunResult:
        try:
            self._conn.execute(f"EXPLAIN {sql}")
            return DryRunResult(estimated_bytes=0, valid=True, error=None)
        except Exception as e:
            return DryRunResult(estimated_bytes=0, valid=False, error=str(e))

    def get_table_schema(self, table_name: str) -> dict[str, str]:
        result = self._conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
            [table_name],
        )
        return {row[0]: row[1] for row in result.fetchall()}
