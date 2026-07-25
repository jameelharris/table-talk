import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from table_talk.gemini_caller import GeminiPermanentError, GeminiTransientError
from table_talk.videos_downloader import DownloadPermanentError
from table_talk.hand_start_processing import (
    PendingHandSetup,
    _find_pending_hand_setups,
    _hallucination_guard,
    check_preconditions,
    process_hand_setup,
    process_pending_hand_setups,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _hand_setup_state(players=None, total_seat_count=6, pot_size_bb=1.5):
    if players is None:
        players = [
            {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
            {"seat_position_label": "UTG", "stack_size": 50.0, "seat_number": 9},
        ]
    return {
        "total_seat_count": total_seat_count,
        "pot_size_bb": pot_size_bb,
        "players": players,
    }


_HS = PendingHandSetup(
    hand_setup_id="clip_001_001",
    clip_id="clip_001",
    video_id="vid_a",
    hand_setup_time_seconds=100,
    hand_setup_state=_hand_setup_state(),
    available_seconds=60,
    raw_lead_gap_seconds=60,
)

_CLIP_RESULT_FOUND = {
    "found": True,
    "timestamp": "01:45",  # 105s, within [100, 160]
    "second_action_timestamp": "01:50",  # 110s
    "seat_position_label": "UTG",
    "action_type": "raise",
    "bet_amount": 3.0,
}

_HOLE_CARDS_RESULT = {
    "players": [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "UTG", "hole_cards": ["2c", "3c"]},
    ]
}


def _fake_extract_frame(_video_uri, _ts, output_path):
    """Side effect for mocked extract_frame — creates the temp file."""
    with open(output_path, "wb") as f:
        f.write(b"\xff\xd8\xff\x00" * 4)


def _run(coro):
    return asyncio.run(coro)


def _mock_bq_client(rows=None):
    if rows is None:
        rows = []
    mock_job = MagicMock()
    mock_job.result.return_value = rows
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client


# ---------------------------------------------------------------------------
# check_preconditions
# ---------------------------------------------------------------------------


def test_check_preconditions_passes_valid_state():
    assert check_preconditions(_hand_setup_state()) is None


def test_check_preconditions_null_stack_size():
    state = _hand_setup_state(players=[
        {"seat_position_label": "BB", "stack_size": None, "seat_number": 1},
    ])
    reason = check_preconditions(state)
    assert reason is not None
    assert "null stack_size" in reason
    assert "BB" in reason


def test_check_preconditions_null_seat_position_label():
    state = _hand_setup_state(players=[
        {"seat_position_label": None, "stack_size": 75.0, "seat_number": 1},
    ])
    reason = check_preconditions(state)
    assert reason is not None
    assert "null seat_position_label" in reason
    assert "75.0" in reason


def test_check_preconditions_both_null_reports_under_null_stack():
    """A player with both null stack and null label is reported by check 1."""
    state = _hand_setup_state(players=[
        {"seat_position_label": None, "stack_size": None, "seat_number": 1},
    ])
    reason = check_preconditions(state)
    assert reason is not None
    assert "null stack_size" in reason


def test_check_preconditions_total_seat_count_too_low():
    state = _hand_setup_state(total_seat_count=1)
    reason = check_preconditions(state)
    assert reason is not None
    assert "total_seat_count" in reason


def test_check_preconditions_total_seat_count_null():
    state = _hand_setup_state(total_seat_count=None)
    reason = check_preconditions(state)
    assert reason is not None
    assert "total_seat_count" in reason


def test_check_preconditions_pot_size_bb_zero_or_null():
    for bad_pot in (0, None):
        state = _hand_setup_state(pot_size_bb=bad_pot)
        reason = check_preconditions(state)
        assert reason is not None
        assert "pot_size_bb" in reason


# ---------------------------------------------------------------------------
# _hallucination_guard
# ---------------------------------------------------------------------------


def test_hallucination_guard_in_window_passes():
    _hallucination_guard(fva_seconds=130, hand_setup_time=100, available_seconds=60)  # no raise


def test_hallucination_guard_boundaries_pass():
    _hallucination_guard(fva_seconds=100, hand_setup_time=100, available_seconds=60)
    _hallucination_guard(fva_seconds=160, hand_setup_time=100, available_seconds=60)


def test_hallucination_guard_out_of_window_raises():
    with pytest.raises(GeminiPermanentError, match="hallucination"):
        _hallucination_guard(fva_seconds=10, hand_setup_time=100, available_seconds=60)


# ---------------------------------------------------------------------------
# _find_pending_hand_setups
# ---------------------------------------------------------------------------


def test_find_pending_hand_setups_no_filters():
    mock_client = _mock_bq_client()
    _find_pending_hand_setups("proj", "ds", client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "hand_setup_processing_attempts" in query
    assert "hand_setups" in query
    assert "failed_transient" in query
    assert "only_video_ids" not in query
    assert "only_hand_setup_ids" not in query

    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert param_names == {"max_available_seconds"}


def test_find_pending_hand_setups_video_filter():
    mock_client = _mock_bq_client()
    _find_pending_hand_setups("proj", "ds", only_video_ids=["v1"], client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "only_video_ids" in query
    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert "only_video_ids" in param_names


def test_find_pending_hand_setups_hand_setup_id_filter():
    mock_client = _mock_bq_client()
    _find_pending_hand_setups("proj", "ds", only_hand_setup_ids=["hs1"], client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "only_hand_setup_ids" in query
    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert "only_hand_setup_ids" in param_names


def test_find_pending_hand_setups_builds_pending_hand_setup():
    row = MagicMock()
    row.hand_setup_id = "hs1"
    row.clip_id = "clip1"
    row.video_id = "vid1"
    row.hand_setup_time_seconds = 100
    row.hand_setup_state = {"players": []}
    row.available_seconds = 60
    row.raw_lead_gap_seconds = 120
    mock_client = _mock_bq_client(rows=[row])

    results = _find_pending_hand_setups("proj", "ds", client=mock_client)

    assert len(results) == 1
    assert results[0] == PendingHandSetup(
        hand_setup_id="hs1",
        clip_id="clip1",
        video_id="vid1",
        hand_setup_time_seconds=100,
        hand_setup_state={"players": []},
        available_seconds=60,
        raw_lead_gap_seconds=120,
    )


# ---------------------------------------------------------------------------
# process_hand_setup — happy path
# ---------------------------------------------------------------------------


def test_process_hand_setup_happy_path():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    assert mock_upload.call_count == 4  # fva + 3 verify frames

    rows_arg = mock_write_starts.call_args[0][0]
    assert len(rows_arg) == 1
    row = rows_arg[0]
    assert row.hand_start_id == "clip_001_001_001"
    assert row.hand_setup_id == "clip_001_001"
    assert row.fva_time_seconds == 105
    assert row.second_action_time_seconds == 110
    assert len(row.verify_frame_gcs_paths) == 3
    assert row.fva_frame_gcs_path == "gs://hand-starts-bucket/vid_a/clip_001/clip_001_001/fva.jpg"

    players = row.hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]
    assert by_label["UTG"]["hole_cards"] == ["2c", "3c"]

    fva = row.hand_start_state["fva"]
    assert fva["seat_position_label"] == "UTG"
    assert fva["seat_number"] == 9

    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert attempt_row.hand_setup_id == "clip_001_001"


def test_process_hand_setup_status_message_notes_capped_window():
    hs = PendingHandSetup(
        hand_setup_id="clip_001_001",
        clip_id="clip_001",
        video_id="vid_a",
        hand_setup_time_seconds=100,
        hand_setup_state=_hand_setup_state(),
        available_seconds=60,
        raw_lead_gap_seconds=200,  # capped: raw > available
    )
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    attempt_row = mock_write_attempt.call_args[0][0]
    assert "capped from raw_lead_gap=200" in attempt_row.status_message


def test_process_hand_setup_hole_card_no_match_is_none():
    hs = PendingHandSetup(
        hand_setup_id="clip_001_001",
        clip_id="clip_001",
        video_id="vid_a",
        hand_setup_time_seconds=100,
        hand_setup_state=_hand_setup_state(players=[
            {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
            {"seat_position_label": "CO", "stack_size": 60.0, "seat_number": 4},
            {"seat_position_label": "UTG", "stack_size": 50.0, "seat_number": 9},
        ]),
        available_seconds=60,
        raw_lead_gap_seconds=60,
    )
    # Gemini's response omits CO — its hole_cards should end up None.
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["CO"]["hole_cards"] is None
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]


# ---------------------------------------------------------------------------
# process_hand_setup — complete_skipped
# ---------------------------------------------------------------------------


def test_process_hand_setup_complete_skipped():
    hs = PendingHandSetup(
        hand_setup_id="clip_001_001",
        clip_id="clip_001",
        video_id="vid_a",
        hand_setup_time_seconds=100,
        hand_setup_state=_hand_setup_state(total_seat_count=1),  # fails precondition
        available_seconds=60,
        raw_lead_gap_seconds=60,
    )
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip") as mock_gemini,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete_skipped"
    mock_gemini.assert_not_called()
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete_skipped"
    assert "total_seat_count" in attempt_row.status_message


# ---------------------------------------------------------------------------
# process_hand_setup — found:false branches
# ---------------------------------------------------------------------------


def test_process_hand_setup_uncontested_is_complete_zero_rows():
    result = {"found": False, "reason": "uncontested"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    mock_extract.assert_not_called()
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "uncontested" in attempt_row.status_message


def test_process_hand_setup_no_first_voluntary_commitment_is_failed_transient():
    result = {"found": False, "reason": "no_first_voluntary_commitment_found"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "no_first_voluntary_commitment_found" in attempt_row.status_message


def test_process_hand_setup_no_second_action_is_failed_transient():
    result = dict(_CLIP_RESULT_FOUND)
    result["second_action_timestamp"] = None
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "no second action" in attempt_row.status_message


# ---------------------------------------------------------------------------
# process_hand_setup — error classification
# ---------------------------------------------------------------------------


def test_process_hand_setup_gemini_transient_error():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip",
              side_effect=GeminiTransientError("rate limited")),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_transient"
    assert mock_attempt.call_args[0][0].status == "failed_transient"


def test_process_hand_setup_gemini_permanent_error_malformed_json():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip",
              side_effect=GeminiPermanentError("malformed JSON")),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_permanent"
    assert mock_attempt.call_args[0][0].status == "failed_permanent"


def test_process_hand_setup_hallucinated_fva_is_failed_permanent():
    result = dict(_CLIP_RESULT_FOUND)
    result["timestamp"] = "00:10"  # 10s, outside [100, 160]
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_permanent"
    mock_extract.assert_not_called()
    mock_write_starts.assert_not_called()
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_permanent"
    assert "hallucination" in attempt_row.status_message


def test_process_hand_setup_hallucinated_fva_wins_over_no_second_action():
    """A hallucinated FVA on a hand that also lacks a second action classifies
    failed_permanent, not failed_transient — the hallucination guard runs first."""
    result = dict(_CLIP_RESULT_FOUND)
    result["timestamp"] = "00:10"  # hallucinated, outside window
    result["second_action_timestamp"] = None  # also missing
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "failed_permanent"
    mock_write_starts.assert_not_called()
    assert mock_attempt.call_args[0][0].status == "failed_permanent"


# ---------------------------------------------------------------------------
# process_hand_setup — atomicity invariant
# ---------------------------------------------------------------------------


def test_process_hand_setup_no_hand_starts_row_unless_complete_with_row():
    """Every non-complete outcome across the suite writes zero hand_starts rows."""
    scenarios = [
        {"found": False, "reason": "uncontested"},
        {"found": False, "reason": "no_first_voluntary_commitment_found"},
        {**_CLIP_RESULT_FOUND, "second_action_timestamp": None},
    ]
    for result in scenarios:
        with (
            patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
            patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
            patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
        ):
            _run(process_hand_setup(
                _HS, "/tmp/video.mp4", "proj", "ds",
                "videos-bucket", "hand-starts-bucket",
                "identify prompt", "extract prompt",
            ))
        mock_write_starts.assert_not_called()


# ---------------------------------------------------------------------------
# process_pending_hand_setups — dispatch logic
# ---------------------------------------------------------------------------


def test_process_pending_hand_setups_dispatch():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60),
        PendingHandSetup("vid_b_001_001", "vid_b_001", "vid_b", 0, {}, 60, 60),
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video") as mock_download,
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock, return_value="complete") as mock_process,
    ):
        stats = _run(process_pending_hand_setups(
            "proj", "ds", "vbucket", "hbucket", "id_prompt", "eh_prompt",
        ))

    assert mock_download.call_count == 2
    downloaded_videos = {c.args[0].split("/")[-1].replace(".mp4", "") for c in mock_download.call_args_list}
    assert downloaded_videos == {"vid_a", "vid_b"}

    assert mock_process.call_count == 3
    assert stats["hand_setups_processed"] == 3
    assert stats["hand_setups_complete"] == 3
    assert stats["hand_setups_failed_transient"] == 0


def test_process_pending_hand_setups_scope_params_translated():
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=[]) as mock_find,
        patch("table_talk.hand_start_processing.download_video"),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock, return_value="complete"),
    ):
        _run(process_pending_hand_setups(
            "proj", "ds", "vb", "hb", "ip", "ep",
            video_id="v1", only_hand_setup_ids=["hs1"],
        ))

    mock_find.assert_called_once_with(
        "proj", "ds",
        only_video_ids=["v1"],
        only_hand_setup_ids=["hs1"],
        client=None,
    )


def test_process_pending_hand_setups_no_video_id_means_no_video_scope():
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=[]) as mock_find,
        patch("table_talk.hand_start_processing.download_video"),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock, return_value="complete"),
    ):
        _run(process_pending_hand_setups("proj", "ds", "vb", "hb", "ip", "ep"))

    mock_find.assert_called_once_with(
        "proj", "ds",
        only_video_ids=None,
        only_hand_setup_ids=None,
        client=None,
    )


def test_process_pending_hand_setups_download_failure_marks_transient():
    hand_setups = [PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60)]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=Exception("network error")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups("proj", "ds", "vb", "hb", "ip", "ep"))

    mock_process.assert_not_called()
    assert stats["hand_setups_processed"] == 1
    assert stats["hand_setups_failed_transient"] == 1
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "video_download_failed" in attempt_row.status_message


def test_process_pending_hand_setups_download_not_found_marks_permanent():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60),
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=DownloadPermanentError("gone")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups("proj", "ds", "vb", "hb", "ip", "ep"))

    mock_process.assert_not_called()
    assert stats["hand_setups_processed"] == 2
    assert stats["hand_setups_failed_permanent"] == 2
    assert mock_attempt.call_count == 2
    for c in mock_attempt.call_args_list:
        row = c.args[0]
        assert row.status == "failed_permanent"
        assert "video_download_not_found" in row.status_message
