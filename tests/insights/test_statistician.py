from table_talk.insights.contracts import (
    AnalysisMethod,
    CanonicalizationChoice,
    CanonicalizationManifest,
    Hypothesis,
)
from table_talk.insights.statistician import compute_statistics


def _make_hypothesis(minimum_sample_per_cell: int = 10) -> Hypothesis:
    return Hypothesis(
        claim="Test claim.",
        primary_metric="shove_rate",
        stratification=["position"],
        minimum_sample_per_cell=minimum_sample_per_cell,
        canonicalization=CanonicalizationManifest(
            choices=[CanonicalizationChoice(
                concept="test",
                chosen_definition="x = 1",
                version="v1",
                rejected_alternatives=[],
            )],
            version="v1",
        ),
        motivation="test motivation",
    )


def test_compute_basic_cells() -> None:
    rows = [
        {"position": "BTN", "metric": 0.72, "sample_size": 50},
        {"position": "BB", "metric": 0.45, "sample_size": 8},
    ]
    results = compute_statistics(rows, _make_hypothesis(minimum_sample_per_cell=10))
    assert len(results.cells) == 2

    btn = next(c for c in results.cells if c.dimensions["position"] == "BTN")
    assert btn.point_estimate == 0.72
    assert btn.sample_size == 50
    assert btn.below_min_sample is False

    bb = next(c for c in results.cells if c.dimensions["position"] == "BB")
    assert bb.sample_size == 8
    assert bb.below_min_sample is True  # 8 < 10


def test_below_min_sample_flag() -> None:
    rows = [{"position": "BTN", "metric": 0.5, "sample_size": 3}]
    results = compute_statistics(rows, _make_hypothesis(minimum_sample_per_cell=5))
    assert results.cells[0].below_min_sample is True

    rows2 = [{"position": "BTN", "metric": 0.5, "sample_size": 5}]
    results2 = compute_statistics(rows2, _make_hypothesis(minimum_sample_per_cell=5))
    assert results2.cells[0].below_min_sample is False  # exactly at threshold


def test_ci_equals_point_estimate_placeholder() -> None:
    # CI is currently a placeholder; ci_low == ci_high == point_estimate
    rows = [{"position": "BTN", "metric": 0.5, "sample_size": 100}]
    results = compute_statistics(rows, _make_hypothesis())
    cell = results.cells[0]
    assert cell.ci_low == cell.point_estimate
    assert cell.ci_high == cell.point_estimate


def test_total_sample_size() -> None:
    rows = [
        {"position": "BTN", "metric": 0.72, "sample_size": 50},
        {"position": "BB", "metric": 0.45, "sample_size": 30},
    ]
    results = compute_statistics(rows, _make_hypothesis())
    assert results.total_sample_size == 80


def test_method_is_sql_only() -> None:
    rows = [{"position": "BTN", "metric": 0.5, "sample_size": 20}]
    results = compute_statistics(rows, _make_hypothesis())
    assert results.method == AnalysisMethod.SQL_ONLY


def test_dimensions_are_strings() -> None:
    rows = [{"position": "BTN", "total_seat_count": 9, "metric": 0.5, "sample_size": 20}]
    results = compute_statistics(rows, _make_hypothesis())
    dims = results.cells[0].dimensions
    assert dims == {"position": "BTN", "total_seat_count": "9"}
    assert isinstance(dims["total_seat_count"], str)


def test_query_metadata_passed_through() -> None:
    rows = [{"position": "BTN", "metric": 0.5, "sample_size": 20}]
    results = compute_statistics(
        rows, _make_hypothesis(), query_metadata={"bytes_processed": 12345}
    )
    assert results.query_metadata == {"bytes_processed": 12345}


def test_empty_query_metadata_default() -> None:
    rows = [{"position": "BTN", "metric": 0.5, "sample_size": 20}]
    results = compute_statistics(rows, _make_hypothesis())
    assert results.query_metadata == {}
