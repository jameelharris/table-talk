import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk._generated.clip_materialization_attempts_row import (
    ClipMaterializationAttemptsRow,
)
from table_talk.clip_materialization_attempts_writer import (
    VALID_STATUSES,
    ClipMaterializationAttemptsWriteError,
    write_clip_materialization_attempt_row,
    write_clip_materialization_attempt_rows,
)


def _sample_row(status="complete", status_message=None):
    return ClipMaterializationAttemptsRow(
        attempt_id="attempt-1",
        video_id="dQw4w9WgXcQ",
        status=status,
        status_message=status_message,
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
    row = _sample_row(status_message="complete: 13 clip windows")

    write_clip_materialization_attempt_row(
        row, project="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert query_str.startswith("INSERT INTO `proj.ds.clip_materialization_attempts`")
    # State tables are append-only: never an UPDATE, never a DELETE.
    assert "DELETE" not in query_str
    assert "UPDATE" not in query_str

    param_names = set(_params(mock_client))
    assert param_names == {"attempt_id", "video_id", "status", "status_message"}
    # attempted_at is server-defaulted and omitted from the dataclass entirely.
    assert "attempted_at" not in param_names
    mock_job.result.assert_called_once()


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_every_valid_status_is_accepted(status):
    mock_client, _ = _mock_client()

    write_clip_materialization_attempt_row(
        _sample_row(status=status), project="proj", dataset="ds", client=mock_client
    )

    assert _params(mock_client)["status"].value == status


def test_valid_statuses_matches_documented_five():
    # The schema's status description enumerates exactly these; if one is added
    # here, add it there too and decide its terminal/retryable category.
    # 'blocked_upstream' is retryable but deliberately not a 'failed%' status,
    # so the consecutive-failure counter's prefix match excludes it.
    assert VALID_STATUSES == frozenset({
        "complete",
        "blocked_upstream",
        "failed_transient",
        "failed_permanent",
        "failed_parked",
    })


def test_invalid_status_raises_before_bq_call():
    mock_client, _ = _mock_client()

    with pytest.raises(ClipMaterializationAttemptsWriteError, match="Invalid status"):
        write_clip_materialization_attempt_row(
            _sample_row(status="in_progress"), project="proj", dataset="ds", client=mock_client
        )

    mock_client.query.assert_not_called()


def test_null_status_message_is_omitted_from_the_insert():
    mock_client, _ = _mock_client()

    write_clip_materialization_attempt_row(
        _sample_row(status_message=None), project="proj", dataset="ds", client=mock_client
    )

    param_names = set(_params(mock_client))
    assert param_names == {"attempt_id", "video_id", "status"}
    assert "status_message" not in mock_client.query.call_args[0][0]


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"reason": "invalid"}])

    with pytest.raises(ClipMaterializationAttemptsWriteError, match="BQ DML errors"):
        write_clip_materialization_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )


def test_google_cloud_error_raises():
    mock_client, _ = _mock_client()
    mock_client.query.side_effect = gcloud_exceptions.GoogleCloudError("boom")

    with pytest.raises(ClipMaterializationAttemptsWriteError, match="boom"):
        write_clip_materialization_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )


def test_client_none_instantiates_with_project():
    with pytest.MonkeyPatch.context() as mp:
        created = {}

        def _fake_client(project=None):
            created["project"] = project
            mock_job = MagicMock()
            mock_job.errors = None
            client = MagicMock()
            client.query.return_value = mock_job
            return client

        mp.setattr(
            "table_talk.clip_materialization_attempts_writer.bigquery.Client",
            _fake_client,
        )
        write_clip_materialization_attempt_row(
            _sample_row(), project="proj", dataset="ds"
        )

    assert created["project"] == "proj"


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with pytest.MonkeyPatch.context() as mp:
        instantiated = []
        mp.setattr(
            "table_talk.clip_materialization_attempts_writer.bigquery.Client",
            lambda **kw: instantiated.append(kw),
        )
        write_clip_materialization_attempt_row(
            _sample_row(), project="proj", dataset="ds", client=mock_client
        )

    assert instantiated == []


# --- integration tests ---


def _cleanup(client, project, dataset, video_id):
    client.query(
        f"DELETE FROM `{project}.{dataset}.clip_materialization_attempts` "
        "WHERE video_id = @val",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("val", "STRING", video_id)]
        ),
    ).result()


@pytest.mark.integration
def test_write_attempt_row_integration_appends_rather_than_replacing():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_mat_{uid}"

    client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.clip_materialization_attempts"

    try:
        for status in ("blocked_upstream", "failed_transient", "complete"):
            write_clip_materialization_attempt_row(
                ClipMaterializationAttemptsRow(
                    attempt_id=uuid.uuid4().hex,
                    video_id=video_id,
                    status=status,
                    status_message=f"integration: {status}",
                ),
                project=project,
                dataset=dataset,
                client=client,
            )

        query = (
            f"SELECT * FROM `{table_ref}` WHERE video_id = @video_id ORDER BY attempted_at"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
        )
        results = list(client.query(query, job_config=job_config).result())

        # Append-only: three attempts, three rows, none superseded in place.
        assert len(results) == 3
        assert [r.status for r in results] == [
            "blocked_upstream",
            "failed_transient",
            "complete",
        ]
        assert len({r.attempt_id for r in results}) == 3
        for r in results:
            assert r.attempted_at is not None
            skew = abs(r.attempted_at.replace(tzinfo=UTC) - datetime.now(UTC))
            assert skew < timedelta(seconds=30)
    finally:
        _cleanup(client, project, dataset, video_id)


# --- batched writes ---
#
# The plural sibling, used by `tt mark-pending` to append one command's marks in
# one INSERT. State tables are append-only, so this is a bare INSERT — there is
# no DELETE and no transaction.

_MARK_MESSAGE = "mark-pending: rebuilding clip_manifest"


def _batch_row(index, status="failed_transient", status_message=_MARK_MESSAGE):
    return ClipMaterializationAttemptsRow(
        attempt_id=f"attempt_{index:03d}",
        video_id=f"dQw4w9WgXcQ_{index:03d}",
        status=status,
        status_message=status_message,
    )


def test_batch_emits_one_insert_with_a_tuple_per_row():
    mock_client, mock_job = _mock_client()

    write_clip_materialization_attempt_rows(
        [_batch_row(0), _batch_row(1)], project="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.clip_materialization_attempts" in query_str
    assert query_str.split("VALUES", 1)[1].count("(") == 2
    param_names = {p.name for p in kwargs["job_config"].query_parameters}
    assert "video_id_0" in param_names
    assert "video_id_1" in param_names
    assert "attempted_at" not in query_str
    mock_job.result.assert_called_once()


def test_batch_empty_list_is_a_noop():
    mock_client, _ = _mock_client()

    write_clip_materialization_attempt_rows([], project="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_not_called()


def test_batch_invalid_status_raises_before_bq_call():
    mock_client, _ = _mock_client()

    with pytest.raises(ClipMaterializationAttemptsWriteError, match="Invalid status"):
        write_clip_materialization_attempt_rows(
            [_batch_row(0), _batch_row(1, status="not_a_real_status")],
            project="proj",
            dataset="ds",
            client=mock_client,
        )

    mock_client.query.assert_not_called()


def test_batch_rows_disagreeing_on_columns_raise():
    # One INSERT carries one column list, and None-valued fields are omitted.
    mock_client, _ = _mock_client()

    with pytest.raises(ClipMaterializationAttemptsWriteError, match="disagree on columns"):
        write_clip_materialization_attempt_rows(
            [_batch_row(0), _batch_row(1, status_message=None)],
            project="proj",
            dataset="ds",
            client=mock_client,
        )

    mock_client.query.assert_not_called()
