from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from table_talk.ingest import Decision, process_manifest, process_url, reconcile_url
from table_talk.manifest import ManifestError, VideoManifestEntry
from table_talk.videos_fetcher import (
    FailureCode,
    FetchResult,
    TerminalFetchError,
    TransientFetchError,
)
from table_talk.videos_uploader import UploadError, UploadResult
from table_talk.videos_writer import VideosWriteError

URL = "https://youtu.be/dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"
PROJECT = "test-project"
DATASET = "test_dataset"
BUCKET = "test-bucket"


def _mock_bq_client(*query_results):
    mock = MagicMock()
    mock.query.side_effect = list(query_results)
    return mock


def _make_fetch_result():
    return FetchResult(
        local_path=Path("/fake/dQw4w9WgXcQ.mp4"),
        video_id=VIDEO_ID,
        title="Test Video",
        duration_seconds=120,
    )


def _make_upload_result():
    return UploadResult(
        gcs_uri=f"gs://{BUCKET}/{VIDEO_ID}.mp4",
        size_bytes=1000,
        already_existed=False,
    )


# --- reconcile_url unit tests ---


def test_reconcile_video_id_in_videos():
    bq = _mock_bq_client([MagicMock()])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.SKIP_ALREADY_COMPLETE


def test_reconcile_no_rows_anywhere():
    bq = _mock_bq_client([], [])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.INGEST


def test_reconcile_latest_attempt_failed_terminal():
    mock_row = MagicMock()
    mock_row.status = "failed_terminal"
    bq = _mock_bq_client([], [mock_row])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.SKIP_TERMINAL_FAILURE


def test_reconcile_latest_attempt_failed_transient_predownload():
    mock_row = MagicMock()
    mock_row.status = "failed_transient_predownload"
    bq = _mock_bq_client([], [mock_row])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.INGEST


def test_reconcile_latest_attempt_failed_transient_postdownload():
    mock_row = MagicMock()
    mock_row.status = "failed_transient_postdownload"
    bq = _mock_bq_client([], [mock_row])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.INGEST


def test_reconcile_complete_no_videos_row():
    mock_row = MagicMock()
    mock_row.status = "complete"
    bq = _mock_bq_client([], [mock_row])
    result = reconcile_url(URL, VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)
    assert result == Decision.INGEST


# --- process_url unit tests ---


def test_process_url_skip_already_complete():
    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.SKIP_ALREADY_COMPLETE),
        patch("table_talk.ingest.fetch_video") as mock_fetch,
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_fetch.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_video.assert_not_called()
    mock_write_attempt.assert_not_called()


def test_process_url_skip_terminal_failure():
    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.SKIP_TERMINAL_FAILURE),
        patch("table_talk.ingest.fetch_video") as mock_fetch,
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_fetch.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_video.assert_not_called()
    mock_write_attempt.assert_not_called()


def test_process_url_happy_path():
    fetch_result = _make_fetch_result()
    upload_result = _make_upload_result()

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", return_value=fetch_result),
        patch("table_talk.ingest.upload_video", return_value=upload_result),
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_write_video.assert_called_once()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert attempt_row.video_id == VIDEO_ID
    assert attempt_row.duration_ms is not None


def test_process_url_terminal_fetch_error():
    exc = TerminalFetchError(FailureCode.VIDEO_UNAVAILABLE, "Video unavailable")

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", side_effect=exc),
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_upload.assert_not_called()
    mock_write_video.assert_not_called()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_terminal"
    assert str(exc) in attempt_row.status_message
    assert attempt_row.video_id is None


def test_process_url_transient_fetch_error():
    exc = TransientFetchError(FailureCode.RATE_LIMITED, "HTTP Error 429")

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", side_effect=exc),
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_upload.assert_not_called()
    mock_write_video.assert_not_called()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient_predownload"


def test_process_url_upload_error():
    fetch_result = _make_fetch_result()
    exc = UploadError("upload failed")

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", return_value=fetch_result),
        patch("table_talk.ingest.upload_video", side_effect=exc),
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_write_video.assert_not_called()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient_predownload"
    assert attempt_row.status_message.startswith("gcs_upload_failed:")
    assert attempt_row.video_id == VIDEO_ID


def test_process_url_write_video_row_error():
    fetch_result = _make_fetch_result()
    upload_result = _make_upload_result()
    exc = VideosWriteError("BQ write failed")

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", return_value=fetch_result),
        patch("table_talk.ingest.upload_video", return_value=upload_result),
        patch("table_talk.ingest.write_video_row", side_effect=exc),
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient_postdownload"
    assert attempt_row.status_message.startswith("bq_write_failed:")
    assert attempt_row.video_id == VIDEO_ID


def test_process_url_invalid_duration():
    fetch_result = FetchResult(
        local_path=Path("/fake/dQw4w9WgXcQ.mp4"),
        video_id=VIDEO_ID,
        title="Test Video",
        duration_seconds=0,
    )

    with (
        patch("table_talk.ingest.extract_video_id", return_value=VIDEO_ID),
        patch("table_talk.ingest.reconcile_url", return_value=Decision.INGEST),
        patch("table_talk.ingest.fetch_video", return_value=fetch_result),
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_video_row") as mock_write_video,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_upload.assert_not_called()
    mock_write_video.assert_not_called()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_terminal"
    assert "invalid_duration" in attempt_row.status_message
    assert attempt_row.video_id == VIDEO_ID


def test_process_url_manifest_error():
    exc = ManifestError("Cannot extract video ID")

    with (
        patch("table_talk.ingest.extract_video_id", side_effect=exc),
        patch("table_talk.ingest.reconcile_url") as mock_reconcile,
        patch("table_talk.ingest.fetch_video") as mock_fetch,
        patch("table_talk.ingest.upload_video") as mock_upload,
        patch("table_talk.ingest.write_attempt_row") as mock_write_attempt,
    ):
        process_url(
            URL,
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    mock_reconcile.assert_not_called()
    mock_fetch.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_attempt.assert_called_once()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_terminal"
    assert attempt_row.status_message.startswith("manifest_error:")
    assert attempt_row.video_id is None


# --- process_manifest unit tests ---


def test_process_manifest_calls_process_url_for_each_entry():
    entries = [
        VideoManifestEntry("https://youtu.be/aaaaaaaaaaa"),
        VideoManifestEntry("https://youtu.be/bbbbbbbbbbb"),
    ]

    with (
        patch("table_talk.ingest.load_manifest", return_value=entries),
        patch("table_talk.ingest.process_url") as mock_process_url,
    ):
        process_manifest(
            Path("fake.yaml"),
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    assert mock_process_url.call_count == 2
    urls_called = [c[0][0] for c in mock_process_url.call_args_list]
    assert "https://youtu.be/aaaaaaaaaaa" in urls_called
    assert "https://youtu.be/bbbbbbbbbbb" in urls_called


def test_process_manifest_continues_on_error():
    entries = [
        VideoManifestEntry("https://youtu.be/aaaaaaaaaaa"),
        VideoManifestEntry("https://youtu.be/bbbbbbbbbbb"),
        VideoManifestEntry("https://youtu.be/ccccccccccc"),
    ]
    call_count = []

    def fake_process(url, **kwargs):
        call_count.append(url)
        if "bbb" in url:
            raise RuntimeError("simulated failure")

    with (
        patch("table_talk.ingest.load_manifest", return_value=entries),
        patch("table_talk.ingest.process_url", side_effect=fake_process),
    ):
        process_manifest(
            Path("fake.yaml"),
            project=PROJECT,
            dataset=DATASET,
            bucket=BUCKET,
            bq_client=MagicMock(),
            storage_client=MagicMock(),
        )

    assert len(call_count) == 3


# --- integration test ---


@pytest.mark.integration
def test_process_url_integration():
    from google.cloud import bigquery as bq_module
    from google.cloud import storage as gcs

    from table_talk.manifest import extract_video_id, load_manifest

    project = "table-talk-497020"
    dataset = "table_talk_dev"
    bucket_name = "table-talk-497020-videos-dev"

    manifest = load_manifest(Path("corpus/videos.yaml"))
    url = manifest[0].source_url
    video_id = extract_video_id(url)

    bq_client = bq_module.Client(project=project)
    storage_client = gcs.Client()

    try:
        process_url(
            url,
            project=project,
            dataset=dataset,
            bucket=bucket_name,
            bq_client=bq_client,
            storage_client=storage_client,
        )

        videos_rows = list(
            bq_client.query(
                f"SELECT video_id FROM `{project}.{dataset}.videos` WHERE video_id = @video_id",
                job_config=bq_module.QueryJobConfig(
                    query_parameters=[
                        bq_module.ScalarQueryParameter("video_id", "STRING", video_id)
                    ]
                ),
            )
        )
        assert len(videos_rows) == 1

        attempt_rows = list(
            bq_client.query(
                f"""SELECT status FROM `{project}.{dataset}.video_ingestion_attempts`
                    WHERE source_url = @source_url AND status = 'complete'
                    ORDER BY attempted_at DESC LIMIT 1""",
                job_config=bq_module.QueryJobConfig(
                    query_parameters=[
                        bq_module.ScalarQueryParameter("source_url", "STRING", url)
                    ]
                ),
            )
        )
        assert len(attempt_rows) == 1
        assert attempt_rows[0].status == "complete"

        blob = storage_client.bucket(bucket_name).blob(f"{video_id}.mp4")
        assert blob.exists()

    finally:
        bq_client.query(
            f"DELETE FROM `{project}.{dataset}.videos` WHERE video_id = @video_id",
            job_config=bq_module.QueryJobConfig(
                query_parameters=[
                    bq_module.ScalarQueryParameter("video_id", "STRING", video_id)
                ]
            ),
        ).result()

        bq_client.query(
            f"DELETE FROM `{project}.{dataset}.video_ingestion_attempts`"
            " WHERE source_url = @source_url",
            job_config=bq_module.QueryJobConfig(
                query_parameters=[
                    bq_module.ScalarQueryParameter("source_url", "STRING", url)
                ]
            ),
        ).result()

        blob = storage_client.bucket(bucket_name).blob(f"{video_id}.mp4")
        if blob.exists():
            blob.delete()
