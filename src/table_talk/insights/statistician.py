from __future__ import annotations

from typing import Any

from table_talk.insights.contracts import (
    AnalysisMethod,
    CellResult,
    Hypothesis,
    ResultsObject,
)


def compute_statistics(
    rows: list[dict[str, Any]],
    hypothesis: Hypothesis,
    query_metadata: dict[str, Any] | None = None,
) -> ResultsObject:
    """
    Convert raw SQL result rows into a statistically annotated ResultsObject.

    Expects each row to have:
    - A column named `metric` (the point estimate as a float)
    - A column named `sample_size` (row count as int)
    - All other columns are treated as stratification dimension values

    CI note: ci_low and ci_high are set equal to point_estimate (placeholder).
    TODO: add Wilson interval for proportions and bootstrap CI for other metrics
    once the metric type is tracked in the Hypothesis contract.
    """
    cells = []
    for row in rows:
        dimensions = {k: str(v) for k, v in row.items() if k not in ("metric", "sample_size")}
        point_estimate = float(row["metric"])
        sample_size = int(row["sample_size"])
        cells.append(
            CellResult(
                dimensions=dimensions,
                point_estimate=point_estimate,
                ci_low=point_estimate,
                ci_high=point_estimate,
                sample_size=sample_size,
                below_min_sample=sample_size < hypothesis.minimum_sample_per_cell,
            )
        )

    return ResultsObject(
        cells=cells,
        total_sample_size=sum(c.sample_size for c in cells),
        method=AnalysisMethod.SQL_ONLY,
        query_metadata=query_metadata or {},
    )
