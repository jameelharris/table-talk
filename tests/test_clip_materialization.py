import uuid
from unittest.mock import MagicMock, call, patch

import pytest
from google.cloud import bigquery

from table_talk.clip_materialization import MaterializeError, materialize_clips, materialize_clips_for_pending_videos
from table_talk.clip_manifest_writer import ClipManifestRow
from table_talk.videos_writer import VideosRow, write_video_row

PROJECT = "test-project"
DATASET = "test_dataset"
VIDEO_ID = "dQw4w9WgXcQ"


def _mock_bq(*query_results):
    mock = MagicMock()
    mock.query.side_effect = list(query_results)
    return mock


def _video_row(duration_seconds):
    mock_row = MagicMock()
    mock_row.duration_seconds = duration_seconds
    return mock_row


# --- materialize_clips unit tests ---


def test_240s_aligned_video():
    bq = _mock_bq([_video_row(480)], [])  # videos query, clip_manifest check

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert len(rows) == 2
    assert rows[0] == ClipManifestRow(clip_id=f"{VIDEO_ID}_001", video_id=VIDEO_ID, clip_start_time=0, clip_end_time=240)
    assert rows[1] == ClipManifestRow(clip_id=f"{VIDEO_ID}_002", video_id=VIDEO_ID, clip_start_time=240, clip_end_time=480)


def test_non_aligned_video():
    bq = _mock_bq([_video_row(300)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert len(rows) == 2
    assert rows[0].clip_start_time == 0 and rows[0].clip_end_time == 240
    assert rows[1].clip_start_time == 240 and rows[1].clip_end_time == 300


def test_video_shorter_than_240s():
    bq = _mock_bq([_video_row(60)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert len(rows) == 1
    assert rows[0].clip_start_time == 0 and rows[0].clip_end_time == 60


def test_long_video_ordinals():
    # 10 clips: 9 full 240s windows + 1 remainder
    bq = _mock_bq([_video_row(2161)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert len(rows) == 10
    assert rows[0].clip_id == f"{VIDEO_ID}_001"
    assert rows[8].clip_id == f"{VIDEO_ID}_009"
    assert rows[9].clip_id == f"{VIDEO_ID}_010"


def test_zero_duration_raises():
    bq = _mock_bq([_video_row(0)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        with pytest.raises(MaterializeError):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()


def test_negative_duration_raises():
    bq = _mock_bq([_video_row(-1)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        with pytest.raises(MaterializeError):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()


def test_video_not_in_videos_table_raises():
    bq = _mock_bq([])  # empty result — video not found

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        with pytest.raises(MaterializeError, match="not found"):
            materialize_clips("unknown_id", project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()


def test_idempotence():
    existing_clip = MagicMock()
    bq = _mock_bq([_video_row(480)], [existing_clip])  # clip_manifest already has rows

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()


def test_clip_id_format():
    bq = _mock_bq([_video_row(300)], [])

    with patch("table_talk.clip_materialization.write_clip_manifest_rows") as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert rows[0].clip_id == f"{VIDEO_ID}_001"
    assert rows[1].clip_id == f"{VIDEO_ID}_002"


# --- materialize_clips_for_pending_videos unit tests ---


def _pending_row(video_id):
    mock_row = MagicMock()
    mock_row.video_id = video_id
    return mock_row


def test_finds_pending_videos():
    pending = [_pending_row("vid_aaa"), _pending_row("vid_bbb")]
    bq = MagicMock()
    bq.query.return_value = pending

    with patch("table_talk.clip_materialization.materialize_clips") as mock_mat:
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_mat.call_count == 2
    called_ids = {c[0][0] for c in mock_mat.call_args_list}
    assert called_ids == {"vid_aaa", "vid_bbb"}


def test_skips_already_materialized():
    # No pending videos returned by query
    bq = MagicMock()
    bq.query.return_value = []

    with patch("table_talk.clip_materialization.materialize_clips") as mock_mat:
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_mat.assert_not_called()


def test_continues_on_per_video_failure():
    pending = [_pending_row("vid_aaa"), _pending_row("vid_bbb"), _pending_row("vid_ccc")]
    bq = MagicMock()
    bq.query.return_value = pending

    def fake_materialize(video_id, **kwargs):
        if video_id == "vid_bbb":
            raise MaterializeError("simulated failure")

    with patch("table_talk.clip_materialization.materialize_clips", side_effect=fake_materialize) as mock_mat:
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_mat.call_count == 3


def test_only_video_ids_scopes_query():
    pending = [_pending_row("vid_aaa"), _pending_row("vid_bbb")]
    bq = MagicMock()
    bq.query.return_value = pending

    with patch("table_talk.clip_materialization.materialize_clips") as mock_mat:
        materialize_clips_for_pending_videos(
            project=PROJECT,
            dataset=DATASET,
            bq_client=bq,
            only_video_ids=["vid_aaa", "vid_bbb"],
        )

    call_args = bq.query.call_args
    query_str = call_args[0][0]
    assert "IN UNNEST(@only_video_ids)" in query_str

    job_config = call_args[1]["job_config"]
    param_names = [p.name for p in job_config.query_parameters]
    assert "only_video_ids" in param_names

    assert mock_mat.call_count == 2
    called_ids = {c[0][0] for c in mock_mat.call_args_list}
    assert called_ids == {"vid_aaa", "vid_bbb"}


def test_no_only_video_ids_scans_all_pending():
    pending = [_pending_row("vid_aaa")]
    bq = MagicMock()
    bq.query.return_value = pending

    with patch("table_talk.clip_materialization.materialize_clips"):
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    call_args = bq.query.call_args
    query_str = call_args[0][0]
    assert "IN UNNEST" not in query_str

    # No job_config kwarg, or job_config is None
    job_config = call_args[1].get("job_config")
    assert job_config is None


# --- integration tests ---


@pytest.mark.integration
def test_materialize_clips_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id = f"test_{uuid.uuid4().hex[:8]}"

    client = bigquery.Client(project=project)
    videos_table = f"{project}.{dataset}.videos"
    manifest_table = f"{project}.{dataset}.clip_manifest"

    try:
        write_video_row(
            VideosRow(
                video_id=video_id,
                source_url=f"https://www.youtube.com/watch?v={video_id}",
                title="Integration Test Video",
                duration_seconds=300,
                gcs_path=f"gs://table-talk-497020-videos-dev/{video_id}.mp4",
                file_size_bytes=12345,
            ),
            project=project,
            dataset=dataset,
            client=client,
        )

        materialize_clips(video_id, project=project, dataset=dataset, bq_client=client)

        rows = list(
            client.query(
                f"SELECT * FROM `{manifest_table}` WHERE video_id = @video_id ORDER BY clip_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
                ),
            ).result()
        )
        assert len(rows) == 2
        assert rows[0].clip_id == f"{video_id}_001"
        assert rows[0].clip_start_time == 0
        assert rows[0].clip_end_time == 240
        assert rows[1].clip_id == f"{video_id}_002"
        assert rows[1].clip_start_time == 240
        assert rows[1].clip_end_time == 300
        assert rows[0].materialized_at is not None

        # Idempotence: second call writes no additional rows
        materialize_clips(video_id, project=project, dataset=dataset, bq_client=client)
        rows_after = list(
            client.query(
                f"SELECT * FROM `{manifest_table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
                ),
            ).result()
        )
        assert len(rows_after) == 2

    finally:
        client.query(
            f"DELETE FROM `{manifest_table}` WHERE video_id = @video_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        ).result()
        client.query(
            f"DELETE FROM `{videos_table}` WHERE video_id = @video_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        ).result()


@pytest.mark.integration
def test_materialize_clips_for_pending_videos_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id_a = f"test_{uuid.uuid4().hex[:8]}"
    video_id_b = f"test_{uuid.uuid4().hex[:8]}"

    client = bigquery.Client(project=project)
    videos_table = f"{project}.{dataset}.videos"
    manifest_table = f"{project}.{dataset}.clip_manifest"

    try:
        for vid, duration in [(video_id_a, 120), (video_id_b, 480)]:
            write_video_row(
                VideosRow(
                    video_id=vid,
                    source_url=f"https://www.youtube.com/watch?v={vid}",
                    title="Integration Test Video",
                    duration_seconds=duration,
                    gcs_path=f"gs://table-talk-497020-videos-dev/{vid}.mp4",
                    file_size_bytes=12345,
                ),
                project=project,
                dataset=dataset,
                client=client,
            )

        materialize_clips_for_pending_videos(
            project=project,
            dataset=dataset,
            bq_client=client,
            only_video_ids=[video_id_a, video_id_b],
        )

        for vid, expected_clips in [(video_id_a, 1), (video_id_b, 2)]:
            rows = list(
                client.query(
                    f"SELECT * FROM `{manifest_table}` WHERE video_id = @video_id",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", vid)]
                    ),
                ).result()
            )
            assert len(rows) == expected_clips, f"{vid}: expected {expected_clips} clips, got {len(rows)}"

    finally:
        for vid in (video_id_a, video_id_b):
            client.query(
                f"DELETE FROM `{manifest_table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", vid)]
                ),
            ).result()
            client.query(
                f"DELETE FROM `{videos_table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", vid)]
                ),
            ).result()
