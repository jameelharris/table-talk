import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.clip_manifest_writer import ClipManifestRow, ClipManifestWriteError, write_clip_manifest_row


def _sample_row() -> ClipManifestRow:
    return ClipManifestRow(
        clip_id="dQw4w9WgXcQ_001",
        video_id="dQw4w9WgXcQ",
        clip_start_time=0,
        clip_end_time=240,
    )


def _mock_client(job_errors=None):
    mock_job = MagicMock()
    mock_job.errors = job_errors
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client, mock_job


# --- unit tests ---


def test_happy_path():
    mock_client, mock_job = _mock_client()
    row = _sample_row()

    write_clip_manifest_row(row, project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.clip_manifest" in query_str
    param_names = {p.name for p in job_config.query_parameters}
    assert param_names == set(asdict(row).keys())
    assert "materialized_at" not in param_names
    mock_job.result.assert_called_once()


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])

    with pytest.raises(ClipManifestWriteError, match="bad schema"):
        write_clip_manifest_row(_sample_row(), project="proj", dataset="ds", client=mock_client)


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    err = gcloud_exceptions.GoogleCloudError("network error")
    mock_client.query.side_effect = err

    with pytest.raises(ClipManifestWriteError, match="network error"):
        write_clip_manifest_row(_sample_row(), project="proj", dataset="ds", client=mock_client)


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()

    with patch("table_talk.clip_manifest_writer.bigquery.Client", return_value=mock_client) as mock_cls:
        write_clip_manifest_row(_sample_row(), project="myproj", dataset="ds")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with patch("table_talk.clip_manifest_writer.bigquery.Client") as mock_cls:
        write_clip_manifest_row(_sample_row(), project="proj", dataset="ds", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_write_clip_manifest_row_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id = f"test_{uuid.uuid4().hex[:8]}"
    clip_id = f"{video_id}_001"
    row = ClipManifestRow(
        clip_id=clip_id,
        video_id=video_id,
        clip_start_time=0,
        clip_end_time=240,
    )

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.clip_manifest"

    try:
        write_clip_manifest_row(row, project=project, dataset=dataset, client=client)

        query = f"SELECT * FROM `{table_ref}` WHERE clip_id = @clip_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("clip_id", "STRING", clip_id)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1
        r = results[0]
        assert r.clip_id == row.clip_id
        assert r.video_id == row.video_id
        assert r.clip_start_time == row.clip_start_time
        assert r.clip_end_time == row.clip_end_time
        assert r.materialized_at is not None
        skew = abs(r.materialized_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)
    finally:
        client.query(
            f"DELETE FROM `{table_ref}` WHERE clip_id = @clip_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("clip_id", "STRING", clip_id)]
            ),
        ).result()
