import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.video_ingestion_attempts_writer import (
    VALID_STATUSES,
    AttemptsWriteError,
    VideoIngestionAttemptsRow,
    write_attempt_row,
)


def _sample_row(**kwargs) -> VideoIngestionAttemptsRow:
    defaults = dict(source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", status="complete")
    return VideoIngestionAttemptsRow(**{**defaults, **kwargs})


def _mock_client(job_errors=None):
    mock_job = MagicMock()
    mock_job.errors = job_errors
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client, mock_job


# --- unit tests ---


def test_happy_path_minimal():
    mock_client, mock_job = _mock_client()
    row = _sample_row()

    write_attempt_row(row, project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.video_ingestion_attempts" in query_str
    param_names = {p.name for p in job_config.query_parameters}
    # None-valued fields are omitted (Option B)
    assert param_names == {"source_url", "status"}
    assert "attempted_at" not in param_names
    mock_job.result.assert_called_once()


def test_happy_path_full():
    mock_client, _ = _mock_client()
    row = _sample_row(
        video_id="dQw4w9WgXcQ",
        status_message="all good",
        duration_ms=5000,
    )

    write_attempt_row(row, project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    _, kwargs = mock_client.query.call_args
    param_names = {p.name for p in kwargs["job_config"].query_parameters}
    assert param_names == {"source_url", "status", "video_id", "status_message", "duration_ms"}


def test_invalid_status_raises_before_bq_call():
    mock_client, _ = _mock_client()

    with pytest.raises(AttemptsWriteError, match="Invalid status"):
        write_attempt_row(
            _sample_row(status="not_a_real_status"),
            project="proj",
            dataset="ds",
            client=mock_client,
        )
    mock_client.query.assert_not_called()


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_all_valid_statuses_accepted(status):
    mock_client, _ = _mock_client()
    write_attempt_row(_sample_row(status=status), project="proj", dataset="ds", client=mock_client)
    mock_client.query.assert_called_once()


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])

    with pytest.raises(AttemptsWriteError, match="bad schema"):
        write_attempt_row(_sample_row(), project="proj", dataset="ds", client=mock_client)


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    err = gcloud_exceptions.GoogleCloudError("network error")
    mock_client.query.side_effect = err

    with pytest.raises(AttemptsWriteError, match="network error"):
        write_attempt_row(_sample_row(), project="proj", dataset="ds", client=mock_client)


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()

    with patch(
        "table_talk.video_ingestion_attempts_writer.bigquery.Client", return_value=mock_client
    ) as mock_cls:
        write_attempt_row(_sample_row(), project="myproj", dataset="ds")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with patch("table_talk.video_ingestion_attempts_writer.bigquery.Client") as mock_cls:
        write_attempt_row(_sample_row(), project="proj", dataset="ds", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_write_attempt_row_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    source_url = f"https://test.example.com/{uuid.uuid4().hex[:8]}"
    row = VideoIngestionAttemptsRow(
        source_url=source_url,
        status="failed_transient_predownload",
        video_id=None,
        status_message="integration test",
        duration_ms=1234,
    )

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.video_ingestion_attempts"

    try:
        write_attempt_row(row, project=project, dataset=dataset, client=client)

        query = f"SELECT * FROM `{table_ref}` WHERE source_url = @source_url"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("source_url", "STRING", source_url)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1
        r = results[0]
        assert r.source_url == row.source_url
        assert r.status == row.status
        assert r.video_id == row.video_id
        assert r.status_message == row.status_message
        assert r.duration_ms == row.duration_ms
        assert r.attempted_at is not None
        skew = abs(r.attempted_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)
    finally:
        delete_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("source_url", "STRING", source_url)]
        )
        client.query(
            f"DELETE FROM `{table_ref}` WHERE source_url = @source_url",
            job_config=delete_config,
        ).result()
