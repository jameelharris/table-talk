import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.tournament_results_writer import (
    TournamentResultsRow,
    TournamentResultsWriteError,
    write_tournament_results,
)


def _sample_panel():
    return {
        "panel_visible": True,
        "has_bounty_column": False,
        "currency_symbol": "$",
        "rows": [
            {"rank": 1, "payout": 62760.03, "payout_marked": True, "bounty": None},
            {"rank": 2, "payout": 54407.04, "payout_marked": True, "bounty": None},
            {"rank": 3, "payout": 49632.96, "payout_marked": True, "bounty": None},
            {"rank": 4, "payout": 26271.31, "payout_marked": False, "bounty": None},
            {"rank": 5, "payout": 18436.34, "payout_marked": False, "bounty": None},
        ],
    }


def _sample_row(video_id: str = "dQw4w9WgXcQ") -> TournamentResultsRow:
    return TournamentResultsRow(
        video_id=video_id,
        bounty_type="none",
        currency_symbol="$",
        frame_timestamp_seconds=5,
        frame_gcs_path=f"gs://tournament-results-bucket/{video_id}/results.jpg",
        tournament_results_state={"panel": _sample_panel()},
    )


def _mock_client(job_errors=None):
    mock_job = MagicMock()
    mock_job.errors = job_errors
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client, mock_job


def _params(mock_client):
    _, kwargs = mock_client.query.call_args
    return {p.name: p for p in kwargs["job_config"].query_parameters}


# --- unit tests ---


def test_happy_path():
    mock_client, mock_job = _mock_client()
    row = _sample_row()

    write_tournament_results(
        [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]

    assert query_str.startswith("BEGIN TRANSACTION")
    begin_idx = query_str.index("BEGIN TRANSACTION")
    delete_idx = query_str.index(
        "DELETE FROM `proj.ds.tournament_results` WHERE video_id = @replace_key"
    )
    insert_idx = query_str.index("INSERT INTO `proj.ds.tournament_results`")
    commit_idx = query_str.index("COMMIT TRANSACTION")
    assert begin_idx < delete_idx < insert_idx < commit_idx

    param_names = {p.name for p in job_config.query_parameters}
    expected = {f"{c}_0" for c in asdict(row).keys()} | {"replace_key"}
    assert param_names == expected
    # detected_at is server-defaulted and omitted from the dataclass entirely.
    assert not any("detected_at" in n for n in param_names)
    mock_job.result.assert_called_once()


def test_empty_list_issues_bare_delete():
    # The zero-row outcomes are exactly the ones that most need the DELETE, so
    # an empty list is not a no-op.
    mock_client, _ = _mock_client()

    write_tournament_results(
        [], video_id="dQw4w9WgXcQ", project_id="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert query_str == "DELETE FROM `proj.ds.tournament_results` WHERE video_id = @replace_key"
    assert "BEGIN TRANSACTION" not in query_str
    assert "INSERT" not in query_str

    params = kwargs["job_config"].query_parameters
    assert len(params) == 1
    assert params[0].name == "replace_key"
    assert params[0].value == "dQw4w9WgXcQ"


def test_mismatched_video_id_raises_no_query_issued():
    mock_client, _ = _mock_client()
    row = _sample_row()

    with pytest.raises(TournamentResultsWriteError, match="video_id"):
        write_tournament_results(
            [row], video_id="some_other_id", project_id="proj", dataset="ds", client=mock_client
        )

    mock_client.query.assert_not_called()


def test_replace_key_is_a_parameter_not_derived_from_rows():
    # The natural key is explicit precisely so the DELETE can fire with no rows.
    mock_client, _ = _mock_client()

    write_tournament_results(
        [], video_id="explicit_key", project_id="proj", dataset="ds", client=mock_client
    )

    assert _params(mock_client)["replace_key"].value == "explicit_key"


def test_json_parameter_type_and_value():
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_tournament_results(
        [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
    )

    param = _params(mock_client)["tournament_results_state_0"]
    assert param.type_ == "JSON"
    assert param.value == row.tournament_results_state


def test_no_double_encoding_of_tournament_results_state():
    # The dict is handed to BQ as-is with type JSON. A json.dumps() here would
    # arrive as a JSON *string* containing JSON — the regression this pins.
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_tournament_results(
        [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
    )

    value = _params(mock_client)["tournament_results_state_0"].value
    assert isinstance(value, dict)
    assert value["panel"]["rows"][0]["payout"] == 62760.03


def test_scalar_parameter_types():
    # Every scalar column must be STRING or INT64: bq_param_type has no FLOAT64
    # branch, so a float column would raise only at the first write.
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_tournament_results(
        [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
    )

    params = _params(mock_client)
    assert params["video_id_0"].type_ == "STRING"
    assert params["bounty_type_0"].type_ == "STRING"
    assert params["currency_symbol_0"].type_ == "STRING"
    assert params["frame_timestamp_seconds_0"].type_ == "INT64"
    assert params["frame_gcs_path_0"].type_ == "STRING"


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"reason": "invalid"}])
    row = _sample_row()

    with pytest.raises(TournamentResultsWriteError, match="BQ DML errors"):
        write_tournament_results(
            [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
        )


def test_google_cloud_error_raises():
    mock_client, _ = _mock_client()
    mock_client.query.side_effect = gcloud_exceptions.GoogleCloudError("boom")
    row = _sample_row()

    with pytest.raises(TournamentResultsWriteError, match="boom"):
        write_tournament_results(
            [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
        )


def test_client_none_instantiates_with_project():
    row = _sample_row()
    with pytest.MonkeyPatch.context() as mp:
        created = {}

        def _fake_client(project=None):
            created["project"] = project
            mock_job = MagicMock()
            mock_job.errors = None
            client = MagicMock()
            client.query.return_value = mock_job
            return client

        mp.setattr("table_talk.tournament_results_writer.bigquery.Client", _fake_client)
        write_tournament_results([row], video_id=row.video_id, project_id="proj", dataset="ds")

    assert created["project"] == "proj"


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()
    row = _sample_row()

    with pytest.MonkeyPatch.context() as mp:
        def _boom(*args, **kwargs):
            raise AssertionError("bigquery.Client must not be constructed")

        mp.setattr("table_talk.tournament_results_writer.bigquery.Client", _boom)
        write_tournament_results(
            [row], video_id=row.video_id, project_id="proj", dataset="ds", client=mock_client
        )

    mock_client.query.assert_called_once()


# --- integration tests ---


def _seed_video(client, project, dataset, video_id):
    """Create the videos row via Phase 1's production writer, per CLAUDE.md's
    cross-phase setup rule."""
    from table_talk.videos_writer import VideosRow, write_video_row

    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Payout extraction writer integration test",
            duration_seconds=3000,
            gcs_path=f"gs://table-talk-videos/{video_id}.mp4",
            file_size_bytes=12345,
        ),
        project=project,
        dataset=dataset,
        client=client,
    )


def _cleanup(client, project, dataset, video_id):
    refs = [
        (f"{project}.{dataset}.tournament_results", "video_id", video_id),
        (f"{project}.{dataset}.videos", "video_id", video_id),
    ]
    for table, col, val in refs:
        client.query(
            f"DELETE FROM `{table}` WHERE {col} = @val",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("val", "STRING", val)]
            ),
        ).result()


@pytest.mark.integration
def test_write_tournament_results_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_payouts_{uid}"

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.tournament_results"

    try:
        _seed_video(client, project, dataset, video_id)

        first_row = _sample_row(video_id)
        write_tournament_results(
            [first_row], video_id=video_id, project_id=project, dataset=dataset, client=client
        )

        query = f"SELECT * FROM `{table_ref}` WHERE video_id = @video_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1

        r = results[0]
        assert r.bounty_type == "none"
        assert r.currency_symbol == "$"
        assert r.frame_timestamp_seconds == 5
        assert r.frame_gcs_path == f"gs://tournament-results-bucket/{video_id}/results.jpg"
        assert r.detected_at is not None
        skew = abs(r.detected_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)

        # The JSON column round-trips as parsed JSON, not as a quoted string,
        # and the floats inside it survive — which is the whole reason payout
        # amounts live in the blob rather than in columns.
        state = r.tournament_results_state
        assert state["panel"]["rows"][0]["payout"] == 62760.03
        assert state["panel"]["rows"][0]["bounty"] is None
        assert state["panel"]["has_bounty_column"] is False

        # Reprocess with the same key: REPLACE, not append. This is the direct
        # regression test for the duplicate-row class of bug.
        second_row = replace(first_row, bounty_type="progressive")
        write_tournament_results(
            [second_row], video_id=video_id, project_id=project, dataset=dataset, client=client
        )

        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1
        assert results[0].bounty_type == "progressive"
    finally:
        _cleanup(client, project, dataset, video_id)


@pytest.mark.integration
def test_write_tournament_results_integration_zero_row_deletes_existing():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_payouts_{uid}"

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.tournament_results"

    try:
        _seed_video(client, project, dataset, video_id)
        write_tournament_results(
            [_sample_row(video_id)],
            video_id=video_id,
            project_id=project,
            dataset=dataset,
            client=client,
        )

        write_tournament_results(
            [], video_id=video_id, project_id=project, dataset=dataset, client=client
        )

        query = f"SELECT * FROM `{table_ref}` WHERE video_id = @video_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
        )
        assert list(client.query(query, job_config=job_config).result()) == []
    finally:
        _cleanup(client, project, dataset, video_id)
