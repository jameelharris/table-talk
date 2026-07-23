import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.hand_setup_processing_attempts_writer import (
    VALID_STATUSES,
    HandSetupProcessingAttemptsRow,
    HandSetupProcessingAttemptsWriteError,
    write_hand_setup_processing_attempt_row,
)


def _sample_row(**kwargs) -> HandSetupProcessingAttemptsRow:
    defaults = dict(attempt_id="attempt_001", hand_setup_id="dQw4w9WgXcQ_001_001", status="complete")
    return HandSetupProcessingAttemptsRow(**{**defaults, **kwargs})


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

    write_hand_setup_processing_attempt_row(row, project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.hand_setup_processing_attempts" in query_str
    param_names = {p.name for p in job_config.query_parameters}
    # None-valued fields are omitted
    assert param_names == {"attempt_id", "hand_setup_id", "status"}
    assert "status_message" not in param_names
    assert "attempted_at" not in param_names
    mock_job.result.assert_called_once()


def test_happy_path_full():
    mock_client, _ = _mock_client()
    row = _sample_row(status_message="precondition failed: null stack")

    write_hand_setup_processing_attempt_row(row, project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    _, kwargs = mock_client.query.call_args
    param_names = {p.name for p in kwargs["job_config"].query_parameters}
    assert param_names == {"attempt_id", "hand_setup_id", "status", "status_message"}


def test_none_status_message_filtered():
    mock_client, _ = _mock_client()
    row = _sample_row(status_message=None)

    write_hand_setup_processing_attempt_row(row, project="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    param_names = {p.name for p in kwargs["job_config"].query_parameters}
    assert "status_message" not in param_names


def test_invalid_status_raises_before_bq_call():
    mock_client, _ = _mock_client()

    with pytest.raises(HandSetupProcessingAttemptsWriteError, match="Invalid status"):
        write_hand_setup_processing_attempt_row(
            _sample_row(status="not_a_real_status"),
            project="proj",
            dataset="ds",
            client=mock_client,
        )
    mock_client.query.assert_not_called()


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_all_valid_statuses_accepted(status):
    mock_client, _ = _mock_client()
    write_hand_setup_processing_attempt_row(
        _sample_row(status=status), project="proj", dataset="ds", client=mock_client
    )
    mock_client.query.assert_called_once()


def test_complete_skipped_is_a_valid_status():
    assert "complete_skipped" in VALID_STATUSES


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])

    with pytest.raises(HandSetupProcessingAttemptsWriteError, match="bad schema"):
        write_hand_setup_processing_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    err = gcloud_exceptions.GoogleCloudError("network error")
    mock_client.query.side_effect = err

    with pytest.raises(HandSetupProcessingAttemptsWriteError, match="network error"):
        write_hand_setup_processing_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()

    with patch(
        "table_talk.hand_setup_processing_attempts_writer.bigquery.Client", return_value=mock_client
    ) as mock_cls:
        write_hand_setup_processing_attempt_row(_sample_row(), project="myproj", dataset="ds")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with patch("table_talk.hand_setup_processing_attempts_writer.bigquery.Client") as mock_cls:
        write_hand_setup_processing_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_write_hand_setup_processing_attempt_row_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    hand_setup_id = f"test_{uuid.uuid4().hex[:8]}_001_001"
    row = HandSetupProcessingAttemptsRow(
        attempt_id=f"attempt_{uuid.uuid4().hex[:8]}",
        hand_setup_id=hand_setup_id,
        status="failed_transient",
        status_message="integration test",
    )

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.hand_setup_processing_attempts"

    try:
        write_hand_setup_processing_attempt_row(row, project=project, dataset=dataset, client=client)

        query = f"SELECT * FROM `{table_ref}` WHERE hand_setup_id = @hand_setup_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("hand_setup_id", "STRING", hand_setup_id)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1
        r = results[0]
        assert r.attempt_id == row.attempt_id
        assert r.status == row.status
        assert r.status_message == row.status_message
        assert r.attempted_at is not None
        skew = abs(r.attempted_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)
    finally:
        client.query(
            f"DELETE FROM `{table_ref}` WHERE hand_setup_id = @hand_setup_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("hand_setup_id", "STRING", hand_setup_id)]
            ),
        ).result()
