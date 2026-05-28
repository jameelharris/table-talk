import pytest

from table_talk.insights.clients.bigquery import (
    DryRunResult,
    FakeBigQueryClient,
    QueryResult,
    RealBigQueryClient,
)


@pytest.fixture(scope="module")
def fake() -> FakeBigQueryClient:
    return FakeBigQueryClient()


# --- fixture loading ---

def test_fake_loads_default_fixture(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT COUNT(*) AS n FROM hands")
    assert result.rows[0]["n"] == 42


# --- execute_query ---

def test_execute_query_returns_query_result(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT * FROM hands LIMIT 1")
    assert isinstance(result, QueryResult)
    assert len(result.rows) == 1


def test_execute_query_row_is_dict(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT * FROM hands LIMIT 1")
    row = result.rows[0]
    assert isinstance(row, dict)
    assert "position" in row
    assert "starting_stack_bb" in row
    assert "fva_action" in row
    assert "won_hand" in row


def test_execute_query_aggregation_shape(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query(
        "SELECT position, COUNT(*) AS cnt FROM hands GROUP BY position ORDER BY position"
    )
    assert len(result.rows) == 6
    positions = {row["position"] for row in result.rows}
    assert positions == {"BB", "BTN", "CO", "MP", "SB", "UTG"}
    assert sum(row["cnt"] for row in result.rows) == 42


def test_execute_query_schema_has_expected_columns(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT position, COUNT(*) AS cnt FROM hands GROUP BY position")
    assert "position" in result.schema
    assert "cnt" in result.schema


def test_execute_query_bytes_processed_is_int(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT * FROM hands LIMIT 5")
    assert isinstance(result.bytes_processed, int)
    assert result.bytes_processed >= 0


def test_execute_query_max_rows_limits_results(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("SELECT * FROM hands", max_rows=3)
    assert len(result.rows) == 3


def test_execute_query_shove_frequency_by_position(fake: FakeBigQueryClient) -> None:
    result = fake.execute_query("""
        SELECT
            position,
            COUNT(*) AS total,
            SUM(CASE WHEN fva_action = 'all_in' THEN 1 ELSE 0 END) AS shoves
        FROM hands
        GROUP BY position
        ORDER BY position
    """)
    assert len(result.rows) == 6
    btn = next(r for r in result.rows if r["position"] == "BTN")
    assert btn["shoves"] == 2
    assert btn["total"] == 7


# --- dry_run_query ---

def test_dry_run_valid_sql(fake: FakeBigQueryClient) -> None:
    result = fake.dry_run_query("SELECT position, COUNT(*) FROM hands GROUP BY position")
    assert isinstance(result, DryRunResult)
    assert result.valid is True
    assert result.error is None
    assert result.estimated_bytes == 0


def test_dry_run_invalid_sql(fake: FakeBigQueryClient) -> None:
    result = fake.dry_run_query("THIS IS NOT VALID SQL AT ALL")
    assert result.valid is False
    assert result.error is not None


# --- get_table_schema ---

def test_get_table_schema_returns_dict(fake: FakeBigQueryClient) -> None:
    schema = fake.get_table_schema("hands")
    assert isinstance(schema, dict)


def test_get_table_schema_expected_columns(fake: FakeBigQueryClient) -> None:
    schema = fake.get_table_schema("hands")
    expected = {"hand_id", "total_seat_count", "pot_size_bb", "position",
                "starting_stack_bb", "fva_action", "fva_size_bb", "won_hand"}
    assert expected.issubset(schema.keys())


# --- RealBigQueryClient ---

def test_real_client_execute_query_raises() -> None:
    with pytest.raises(NotImplementedError):
        RealBigQueryClient().execute_query("SELECT 1")


def test_real_client_dry_run_raises() -> None:
    with pytest.raises(NotImplementedError):
        RealBigQueryClient().dry_run_query("SELECT 1")


def test_real_client_get_schema_raises() -> None:
    with pytest.raises(NotImplementedError):
        RealBigQueryClient().get_table_schema("any_table")
