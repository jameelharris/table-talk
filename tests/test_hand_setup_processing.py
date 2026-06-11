import asyncio
import os
import subprocess
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from google.cloud import bigquery

from table_talk.frame_extractor import FrameExtractionError
from table_talk.gemini_caller import GeminiPermanentError, GeminiTransientError
from table_talk.hand_setup_processing import (
    _find_pending_clips,
    _parse_timestamp,
    process_clip,
    process_pending_clips,
)
from table_talk._generated.clip_manifest_row import ClipManifestRow
from table_talk._generated.clip_processing_attempts_row import ClipProcessingAttemptsRow

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CLIP = ClipManifestRow(
    clip_id="dQw4w9WgXcQ_001",
    video_id="dQw4w9WgXcQ",
    clip_start_time=0,
    clip_end_time=240,
)

_PLAYER_INFO = {
    "hand_setup": {
        "total_seat_count": 6,
        "pot_size_bb": 1.5,
        "players": [{"seat_position_label": "BB", "stack_size": 100.0}],
    }
}

_CLIP_RESULT_ONE_SETUP = {
    "hand_setups": [
        {
            "timestamp": "03:00",
            "pot_size_bb": 1.5,
            "community_cards_visible": 0,
            "both_blinds_posted": True,
        }
    ]
}

_CLIP_RESULT_EMPTY = {"hand_setups": []}


def _fake_extract_frame(video_uri, ts, output_path):
    """Side effect for mocked extract_frame — creates the temp file."""
    with open(output_path, "wb") as f:
        f.write(b"\xff\xd8\xff\x00" * 4)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("s,expected", [
    ("05:32", 332),
    ("00:00", 0),
    ("59:59", 3599),
    ("01:00:00", 3600),
    ("02:30:45", 9045),
    ("23:59:59", 86399),
])
def test_parse_timestamp_valid(s, expected):
    assert _parse_timestamp(s) == expected


@pytest.mark.parametrize("s", [
    "",
    "5:32:10:00",
    "05",
    "5.5",
    "5:abc",
    "-5:32",
])
def test_parse_timestamp_invalid(s):
    with pytest.raises(ValueError):
        _parse_timestamp(s)


# ---------------------------------------------------------------------------
# _find_pending_clips
# ---------------------------------------------------------------------------


def _mock_bq_client(rows=None):
    if rows is None:
        rows = []
    mock_job = MagicMock()
    mock_job.result.return_value = rows
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client


def test_find_pending_clips_no_filters():
    mock_client = _mock_bq_client()
    _find_pending_clips("proj", "ds", client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "clip_processing_attempts" in query
    assert "clip_manifest" in query
    assert "failed_transient" in query
    # No scope filters when both are None
    assert "only_clip_ids" not in query
    assert "only_video_ids" not in query
    # No query_parameters passed when no filters
    assert mock_client.query.call_args[1].get("job_config") is None


def test_find_pending_clips_clip_id_filter():
    mock_client = _mock_bq_client()
    _find_pending_clips("proj", "ds", only_clip_ids=["clip_001"], client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "only_clip_ids" in query
    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert "only_clip_ids" in param_names


def test_find_pending_clips_video_id_filter():
    mock_client = _mock_bq_client()
    _find_pending_clips("proj", "ds", only_video_ids=["vid_001"], client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "only_video_ids" in query
    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert "only_video_ids" in param_names


def test_find_pending_clips_both_filters():
    mock_client = _mock_bq_client()
    _find_pending_clips(
        "proj", "ds",
        only_clip_ids=["c1"], only_video_ids=["v1"],
        client=mock_client,
    )
    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert param_names == {"only_clip_ids", "only_video_ids"}


# ---------------------------------------------------------------------------
# process_clip — happy path (one hand setup)
# ---------------------------------------------------------------------------


def test_process_clip_happy_path():
    call_order = []

    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_ONE_SETUP),
        patch("table_talk.hand_setup_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_setup_processing.call_gemini_for_frame", return_value=_PLAYER_INFO),
        patch("table_talk.hand_setup_processing.upload_frame", side_effect=lambda *a, **k: call_order.append("upload")),
        patch("table_talk.hand_setup_processing.write_hand_setups", side_effect=lambda *a, **k: call_order.append("write_setups")) as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row", side_effect=lambda *a, **k: call_order.append("write_attempt")) as mock_write_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    # Upload before batch insert, attempt row last
    assert call_order == ["upload", "write_setups", "write_attempt"]

    # Attempt row has correct status
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "1 hand_setups" in attempt_row.status_message

    # hand_setups rows written
    rows_arg = mock_write_setups.call_args[0][0]
    assert len(rows_arg) == 1
    assert rows_arg[0].hand_setup_id == "dQw4w9WgXcQ_001_001"
    assert rows_arg[0].hand_setup_time_seconds == 180  # 03:00
    assert rows_arg[0].hand_setup_state["total_seat_count"] == 6


# ---------------------------------------------------------------------------
# process_clip — empty hand_setups case
# ---------------------------------------------------------------------------


def test_process_clip_empty_hand_setups():
    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_EMPTY),
        patch("table_talk.hand_setup_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_setup_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_setup_processing.write_hand_setups") as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    mock_extract.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_setups.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "No hand setups" in attempt_row.status_message


# ---------------------------------------------------------------------------
# process_clip — error classification
# ---------------------------------------------------------------------------


def test_process_clip_gemini_transient_error():
    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip",
              side_effect=GeminiTransientError("rate limited")),
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    assert mock_attempt.call_args[0][0].status == "failed_transient"


def test_process_clip_gemini_permanent_error():
    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip",
              side_effect=GeminiPermanentError("safety block")),
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_permanent"
    assert mock_attempt.call_args[0][0].status == "failed_permanent"


def test_process_clip_frame_extraction_error_no_uploads_no_writes():
    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip",
              return_value=_CLIP_RESULT_ONE_SETUP),
        patch("table_talk.hand_setup_processing.extract_frame",
              side_effect=FrameExtractionError("ffmpeg failed")),
        patch("table_talk.hand_setup_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_setup_processing.write_hand_setups") as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    mock_upload.assert_not_called()
    mock_write_setups.assert_not_called()
    assert mock_attempt.call_args[0][0].status == "failed_transient"


def test_process_clip_hallucinated_timestamp_outside_range():
    # Clip range is [0, 240]; LLM returns timestamp 999s — outside range.
    out_of_range_result = {
        "hand_setups": [
            {"timestamp": "16:39", "pot_size_bb": 1.5, "community_cards_visible": 0, "both_blinds_posted": True},
        ]
    }  # 16:39 = 999s, well outside [0, 240]

    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip", return_value=out_of_range_result),
        patch("table_talk.hand_setup_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_setup_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_setup_processing.write_hand_setups") as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_permanent"
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_permanent"
    assert "hallucination" in attempt_row.status_message
    mock_extract.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_setups.assert_not_called()


def test_process_clip_gemini_frame_error_no_inserts_no_uploads():
    two_setups = {
        "hand_setups": [
            {"timestamp": "01:00", "pot_size_bb": 1.5, "community_cards_visible": 0, "both_blinds_posted": True},
            {"timestamp": "02:00", "pot_size_bb": 1.5, "community_cards_visible": 0, "both_blinds_posted": True},
        ]
    }
    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip", return_value=two_setups),
        patch("table_talk.hand_setup_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_setup_processing.call_gemini_for_frame",
              side_effect=GeminiTransientError("rate limited")),
        patch("table_talk.hand_setup_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_setup_processing.write_hand_setups") as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    mock_upload.assert_not_called()
    mock_write_setups.assert_not_called()
    assert mock_attempt.call_args[0][0].status == "failed_transient"


# ---------------------------------------------------------------------------
# process_clip — seat enrichment applied before writing HandSetupsRow
# ---------------------------------------------------------------------------


def test_process_clip_seat_enrichment_applied():
    """Players reaching HandSetupsRow must have seat_number and be sorted."""
    player_info_multi = {
        "hand_setup": {
            "total_seat_count": 6,
            "pot_size_bb": 1.5,
            "players": [
                {"seat_position_label": "UTG", "stack_size": 50.0},
                {"seat_position_label": "BB", "stack_size": 100.0},
            ],
        }
    }

    with (
        patch("table_talk.hand_setup_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_ONE_SETUP),
        patch("table_talk.hand_setup_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_setup_processing.call_gemini_for_frame", return_value=player_info_multi),
        patch("table_talk.hand_setup_processing.upload_frame"),
        patch("table_talk.hand_setup_processing.write_hand_setups") as mock_write_setups,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row"),
    ):
        _run(process_clip(
            _CLIP, "/tmp/video.mp4", "proj", "ds",
            "hand-setups-bucket", "videos-bucket",
            "identify prompt", "extract prompt",
        ))

    rows_arg = mock_write_setups.call_args[0][0]
    players = rows_arg[0].hand_setup_state["players"]

    # Every player with a known label has a non-None seat_number
    assert all(p["seat_number"] is not None for p in players)

    # Players are sorted ascending by seat_number
    seat_numbers = [p["seat_number"] for p in players]
    assert seat_numbers == sorted(seat_numbers)

    # BB (seat 1) sorts before UTG (seat 9)
    assert players[0]["seat_position_label"] == "BB"
    assert players[1]["seat_position_label"] == "UTG"


# ---------------------------------------------------------------------------
# process_pending_clips — dispatch logic
# ---------------------------------------------------------------------------


def test_process_pending_clips_dispatch():
    clips = [
        ClipManifestRow("vid_a_001", "vid_a", 0, 240),
        ClipManifestRow("vid_a_002", "vid_a", 240, 480),
        ClipManifestRow("vid_b_001", "vid_b", 0, 240),
    ]

    with (
        patch("table_talk.hand_setup_processing._find_pending_clips", return_value=clips),
        patch("table_talk.hand_setup_processing.download_video") as mock_download,
        patch("table_talk.hand_setup_processing.process_clip", new_callable=AsyncMock, return_value="complete") as mock_process,
    ):
        stats = _run(process_pending_clips(
            "proj", "ds", "vbucket", "hbucket", "id_prompt", "ep_prompt",
        ))

    # One download per video
    assert mock_download.call_count == 2
    downloaded_videos = {call.args[0].split("/")[-1].replace(".mp4", "") for call in mock_download.call_args_list}
    assert downloaded_videos == {"vid_a", "vid_b"}

    # One process_clip call per clip
    assert mock_process.call_count == 3

    # Stats correct
    assert stats["clips_processed"] == 3
    assert stats["clips_complete"] == 3
    assert stats["clips_failed_transient"] == 0


def test_process_pending_clips_scope_params_propagated():
    with (
        patch("table_talk.hand_setup_processing._find_pending_clips", return_value=[]) as mock_find,
        patch("table_talk.hand_setup_processing.download_video"),
        patch("table_talk.hand_setup_processing.process_clip", new_callable=AsyncMock, return_value="complete"),
    ):
        _run(process_pending_clips(
            "proj", "ds", "vb", "hb", "ip", "ep",
            only_clip_ids=["c1"], only_video_ids=["v1"],
        ))

    mock_find.assert_called_once_with(
        "proj", "ds",
        only_clip_ids=["c1"],
        only_video_ids=["v1"],
    )


def test_process_pending_clips_download_failure_marks_clips_failed():
    clips = [ClipManifestRow("vid_a_001", "vid_a", 0, 240)]

    with (
        patch("table_talk.hand_setup_processing._find_pending_clips", return_value=clips),
        patch("table_talk.hand_setup_processing.download_video",
              side_effect=Exception("network error")),
        patch("table_talk.hand_setup_processing.process_clip", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_setup_processing.write_clip_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_clips(
            "proj", "ds", "vb", "hb", "ip", "ep",
        ))

    mock_process.assert_not_called()
    assert stats["clips_processed"] == 1
    assert stats["clips_failed_transient"] == 1
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "video_download_failed" in attempt_row.status_message


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_process_pending_clips_integration():
    asyncio.run(_integration_body())


async def _integration_body():
    from google.cloud import bigquery as bq
    from google.cloud import storage as gcs

    from table_talk.clip_manifest_writer import ClipManifestRow as CMRow, write_clip_manifest_rows
    from table_talk.videos_writer import VideosRow, write_video_row

    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:10]
    video_id = f"test_p3_{uid}"
    clip_id = f"{video_id}_001"

    videos_bucket = "table-talk-497020-videos-dev"
    hand_setups_bucket = "table-talk-497020-hand-setups-dev"

    bq_client = bq.Client(project=project)
    gcs_client = gcs.Client()

    videos_ref = f"{project}.{dataset}.videos"
    clip_ref = f"{project}.{dataset}.clip_manifest"
    attempts_ref = f"{project}.{dataset}.clip_processing_attempts"
    hand_setups_ref = f"{project}.{dataset}.hand_setups"

    # Generate a small test video via ffmpeg (lavfi testsrc, 60 seconds)
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_path = os.path.join(tmpdir, "fixture.mp4")
        subprocess.run(
            [
                "ffmpeg", "-f", "lavfi",
                "-i", "testsrc=duration=60:size=320x240",
                "-y", fixture_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(fixture_path, "rb") as f:
            video_bytes = f.read()

    # Upload test video to GCS
    video_blob = gcs_client.bucket(videos_bucket).blob(f"{video_id}.mp4")
    video_blob.upload_from_string(video_bytes, content_type="video/mp4")

    # Write setup rows via production writers
    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Phase 3 Integration Test Video",
            duration_seconds=60,
            gcs_path=f"gs://{videos_bucket}/{video_id}.mp4",
            file_size_bytes=len(video_bytes),
        ),
        project=project,
        dataset=dataset,
        client=bq_client,
    )
    write_clip_manifest_rows(
        [CMRow(clip_id=clip_id, video_id=video_id, clip_start_time=0, clip_end_time=60)],
        project=project,
        dataset=dataset,
        client=bq_client,
    )

    prompts_dir = __import__("pathlib").Path(__file__).resolve().parents[1] / "prompts"
    identify_hand_prompt = (prompts_dir / "identify_hand.md").read_text()
    extract_player_info_prompt = (prompts_dir / "extract_player_info.md").read_text()

    try:
        stats = await process_pending_clips(
            project_id=project,
            dataset=dataset,
            videos_bucket=videos_bucket,
            hand_setups_bucket=hand_setups_bucket,
            identify_hand_prompt=identify_hand_prompt,
            extract_player_info_prompt=extract_player_info_prompt,
            only_clip_ids=[clip_id],
        )

        assert stats["clips_processed"] == 1, f"Expected 1 clip processed, got {stats}"

        # Verify attempt row exists (most recent if there are multiple)
        attempt_rows = list(bq_client.query(
            f"SELECT status, status_message FROM `{attempts_ref}` "
            f"WHERE clip_id = @clip_id "
            f"ORDER BY attempted_at DESC LIMIT 1",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("clip_id", "STRING", clip_id)]
            ),
        ).result())
        assert len(attempt_rows) == 1, "No attempt rows written"

        latest = attempt_rows[0]
        # Lavfi fixture has no poker content. Gemini's response is stochastic:
        #   - complete: Gemini returns {"hand_setups": []} as instructed
        #   - failed_permanent: Gemini ignores JSON instruction (malformed JSON) OR
        #                       hallucinates a timestamp outside the clip's range
        # Both prove the orchestration chain ran end-to-end correctly.
        assert latest.status in ("complete", "failed_permanent"), (
            f"Unexpected status {latest.status!r}: {latest.status_message}"
        )

        # Critical atomicity invariant: failure → zero hand_setups rows in BQ.
        hand_setup_count = list(bq_client.query(
            f"SELECT COUNT(*) AS n FROM `{hand_setups_ref}` WHERE clip_id = @clip_id",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("clip_id", "STRING", clip_id)]
            ),
        ).result())[0].n

        if latest.status != "complete":
            assert hand_setup_count == 0, (
                f"Atomicity violation: status={latest.status} but {hand_setup_count} hand_setups rows exist"
            )

    finally:
        # Cleanup in reverse dependency order
        for table, col, val in [
            (hand_setups_ref, "clip_id", clip_id),
            (attempts_ref, "clip_id", clip_id),
            (clip_ref, "clip_id", clip_id),
            (videos_ref, "video_id", video_id),
        ]:
            col_type = "STRING"
            bq_client.query(
                f"DELETE FROM `{table}` WHERE {col} = @val",
                job_config=bq.QueryJobConfig(
                    query_parameters=[bq.ScalarQueryParameter("val", col_type, val)]
                ),
            ).result()

        # Delete test video from GCS
        if video_blob.exists():
            video_blob.delete()

        # Delete any frame objects from hand_setups bucket
        for blob in gcs_client.bucket(hand_setups_bucket).list_blobs(prefix=f"{video_id}/"):
            blob.delete()
