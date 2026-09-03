import asyncio
import os
import subprocess
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from table_talk.gemini_caller import GeminiPermanentError, GeminiTransientError
from table_talk.videos_downloader import DownloadPermanentError
from table_talk.hand_start_processing import (
    PendingHandSetup,
    _find_pending_hand_setups,
    _hallucination_guard,
    _transient_status,
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
    consecutive_failures=0,
)

_CLIP_RESULT_FOUND = {
    "found": True,
    "timestamp": "01:45",  # 105s, within [100, 160]
    "second_action_timestamp": "01:50",  # 110s
    "seat_position_label": "UTG",
    "action_type": "raise",
    "bet_amount": 3.0,
}

_CLIP_RESULT_FVA_CO = {
    "found": True,
    "timestamp": "01:45",
    "second_action_timestamp": "01:50",
    "seat_position_label": "CO",  # seat_number 4 — UTG (seat 9) is non-eligible
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
# _transient_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("consecutive_failures,expected", [
    (0, "failed_transient"),
    (1, "failed_transient"),
    (2, "failed_parked"),
    (5, "failed_parked"),
])
def test_transient_status_boundary(consecutive_failures, expected):
    assert _transient_status(consecutive_failures, max_attempts=3) == expected


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
    row.consecutive_failures = 3
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
        consecutive_failures=3,
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

    assert mock_write_starts.call_args.kwargs["hand_setup_id"] == "clip_001_001"
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
        consecutive_failures=0,
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
        consecutive_failures=0,
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
# process_hand_setup — step C bounded retry for eligible-seat nulls
# ---------------------------------------------------------------------------


def _three_seat_hs():
    return PendingHandSetup(
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
        consecutive_failures=0,
    )


def test_process_hand_setup_retry_fills_eligible_null():
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "UTG", "hole_cards": ["2c", "3c"]},
            # CO omitted -> null on first call
        ]
    }
    second_response = {
        "players": [
            {"seat_position_label": "CO", "hole_cards": ["Th", "9h"]},
        ]
    }
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            side_effect=[first_response, second_response],
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 2
    # Retry must reuse the exact same prompt/frame/args as the first call.
    assert mock_gemini_frame.call_args_list[0] == mock_gemini_frame.call_args_list[1]

    mock_write_starts.assert_called_once()
    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]
    assert by_label["CO"]["hole_cards"] == ["Th", "9h"]
    assert by_label["UTG"]["hole_cards"] == ["2c", "3c"]

    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "null hole_cards" not in attempt_row.status_message


def test_process_hand_setup_retry_still_null_is_still_complete():
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "UTG", "hole_cards": ["2c", "3c"]},
        ]
    }
    second_response = {"players": []}  # retry also misses CO
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            side_effect=[first_response, second_response],
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 2
    mock_write_starts.assert_called_once()

    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["CO"]["hole_cards"] is None

    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "null hole_cards after retry" in attempt_row.status_message
    assert "CO" in attempt_row.status_message


def test_process_hand_setup_retry_does_not_clobber_first_call_answer():
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "UTG", "hole_cards": ["2c", "3c"]},
            # CO omitted -> null, triggers retry
        ]
    }
    second_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": None},  # simulated flip: null this time
            {"seat_position_label": "CO", "hole_cards": ["Th", "9h"]},
        ]
    }
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            side_effect=[first_response, second_response],
        ) as mock_gemini_frame,
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
    assert mock_gemini_frame.call_count == 2

    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]  # retained, not clobbered by retry's null
    assert by_label["CO"]["hole_cards"] == ["Th", "9h"]  # filled by retry
    assert by_label["UTG"]["hole_cards"] == ["2c", "3c"]


def test_process_hand_setup_no_retry_when_first_call_fully_populated():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            return_value=_HOLE_CARDS_RESULT,
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 1

    attempt_row = mock_write_attempt.call_args[0][0]
    assert "null hole_cards" not in attempt_row.status_message


def test_process_hand_setup_non_eligible_null_does_not_trigger_retry():
    hs = _three_seat_hs()
    # FVA is CO (seat 4) -> UTG (seat 9) is non-eligible, and it's the one that's null.
    result = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "CO", "hole_cards": ["2c", "3c"]},
            # UTG omitted -> null, but non-eligible
        ]
    }
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FVA_CO),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            return_value=result,
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 1

    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["UTG"]["hole_cards"] is None

    attempt_row = mock_write_attempt.call_args[0][0]
    assert "null hole_cards" not in attempt_row.status_message


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
        consecutive_failures=0,
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


def test_process_hand_setup_uncontested_is_complete_uncontested_zero_rows():
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

    assert outcome == "complete_uncontested"
    mock_extract.assert_not_called()
    mock_write_starts.assert_called_once_with(
        [], hand_setup_id="clip_001_001", project_id="proj", dataset="ds"
    )
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete_uncontested"
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
# process_hand_setup — retry cap (failed_parked)
# ---------------------------------------------------------------------------

_HS_AT_CAP = replace(_HS, consecutive_failures=2)


def test_process_hand_setup_no_first_voluntary_commitment_parks_at_cap():
    result = {"found": False, "reason": "no_first_voluntary_commitment_found"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            max_attempts=3,
        ))

    assert outcome == "failed_parked"
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_parked"
    assert "no_first_voluntary_commitment_found" in attempt_row.status_message


def test_process_hand_setup_no_second_action_parks_at_cap():
    result = dict(_CLIP_RESULT_FOUND)
    result["second_action_timestamp"] = None
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            max_attempts=3,
        ))

    assert outcome == "failed_parked"
    mock_write_starts.assert_not_called()
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_parked"
    assert "no second action" in attempt_row.status_message


def test_process_hand_setup_catch_all_exception_parks_at_cap():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip",
              side_effect=RuntimeError("boom")),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            max_attempts=3,
        ))

    assert outcome == "failed_parked"
    assert mock_attempt.call_args[0][0].status == "failed_parked"


def test_process_hand_setup_gemini_permanent_error_unaffected_by_cap():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip",
              side_effect=GeminiPermanentError("malformed JSON")),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            max_attempts=3,
        ))

    assert outcome == "failed_permanent"
    assert mock_attempt.call_args[0][0].status == "failed_permanent"


def test_process_hand_setup_complete_uncontested_unaffected_by_cap():
    result = {"found": False, "reason": "uncontested"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=result),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            max_attempts=3,
        ))

    assert outcome == "complete_uncontested"
    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete_uncontested"


# ---------------------------------------------------------------------------
# process_hand_setup — atomicity invariant
# ---------------------------------------------------------------------------


def test_process_hand_setup_no_hand_starts_row_unless_complete_with_row():
    """Non-complete outcomes never call write_hand_starts; uncontested
    (complete_uncontested, zero rows) calls it with an empty list to clear any
    stale row from a prior run."""
    scenarios = [
        ({"found": False, "reason": "uncontested"}, True),
        ({"found": False, "reason": "no_first_voluntary_commitment_found"}, False),
        ({**_CLIP_RESULT_FOUND, "second_action_timestamp": None}, False),
    ]
    for result, expect_zero_row_call in scenarios:
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
        if expect_zero_row_call:
            mock_write_starts.assert_called_once_with(
                [], hand_setup_id="clip_001_001", project_id="proj", dataset="ds"
            )
        else:
            mock_write_starts.assert_not_called()


# ---------------------------------------------------------------------------
# process_pending_hand_setups — dispatch logic
# ---------------------------------------------------------------------------


def test_process_pending_hand_setups_dispatch():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60, 0),
        PendingHandSetup("vid_b_001_001", "vid_b_001", "vid_b", 0, {}, 60, 60, 0),
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
    hand_setups = [PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0)]
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
    assert stats["hand_setups_failed_parked"] == 0
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "video_download_failed" in attempt_row.status_message


def test_process_pending_hand_setups_download_failure_parks_at_cap():
    hand_setups = [PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 2)]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=Exception("network error")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups("proj", "ds", "vb", "hb", "ip", "ep", max_attempts=3))

    mock_process.assert_not_called()
    assert stats["hand_setups_failed_parked"] == 1
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_parked"


def test_process_pending_hand_setups_download_not_found_marks_permanent():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60, 0),
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


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_process_pending_hand_setups_integration():
    asyncio.run(_integration_body())


async def _integration_body():
    from google.cloud import bigquery as bq
    from google.cloud import storage as gcs

    from table_talk._generated.hand_setups_row import HandSetupsRow
    from table_talk.clip_manifest_writer import ClipManifestRow as CMRow, write_clip_manifest_rows
    from table_talk.hand_setups_writer import write_hand_setups
    from table_talk.videos_writer import VideosRow, write_video_row

    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:10]
    video_id = f"test_p4_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"

    videos_bucket = "table-talk-497020-videos-dev"
    hand_setups_bucket = "table-talk-497020-hand-setups-dev"
    hand_starts_bucket = "table-talk-497020-hand-starts-dev"

    bq_client = bq.Client(project=project)
    gcs_client = gcs.Client()

    videos_ref = f"{project}.{dataset}.videos"
    clip_ref = f"{project}.{dataset}.clip_manifest"
    hand_setups_ref = f"{project}.{dataset}.hand_setups"
    attempts_ref = f"{project}.{dataset}.hand_setup_processing_attempts"
    hand_starts_ref = f"{project}.{dataset}.hand_starts"

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

    # Write setup rows via production writers (Phase 1, Phase 2, Phase 3)
    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Phase 4 Integration Test Video",
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
    write_hand_setups(
        [
            HandSetupsRow(
                hand_setup_id=hand_setup_id,
                clip_id=clip_id,
                video_id=video_id,
                hand_setup_time_seconds=0,
                frame_gcs_path=f"gs://{hand_setups_bucket}/{video_id}/{clip_id}/{hand_setup_id}.jpg",
                hand_setup_state={
                    "total_seat_count": 6,
                    "pot_size_bb": 1.5,
                    "players": [
                        {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
                        {"seat_position_label": "UTG", "stack_size": 100.0, "seat_number": 9},
                    ],
                },
            )
        ],
        clip_id=clip_id,
        project_id=project,
        dataset=dataset,
        client=bq_client,
    )

    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    identify_hand_start_prompt = (prompts_dir / "identify_hand_start.md").read_text()
    extract_hole_cards_prompt = (prompts_dir / "extract_hole_cards.md").read_text()

    try:
        stats = await process_pending_hand_setups(
            project_id=project,
            dataset=dataset,
            videos_bucket=videos_bucket,
            hand_starts_bucket=hand_starts_bucket,
            identify_hand_start_prompt=identify_hand_start_prompt,
            extract_hole_cards_prompt=extract_hole_cards_prompt,
            only_hand_setup_ids=[hand_setup_id],
            bq_client=bq_client,
            gcs_client=gcs_client,
        )

        assert stats["hand_setups_processed"] == 1, f"Expected 1 hand_setup processed, got {stats}"

        # Verify attempt row exists (most recent if there are multiple)
        attempt_rows = list(bq_client.query(
            f"SELECT status, status_message FROM `{attempts_ref}` "
            f"WHERE hand_setup_id = @hand_setup_id "
            f"ORDER BY attempted_at DESC LIMIT 1",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("hand_setup_id", "STRING", hand_setup_id)]
            ),
        ).result())
        assert len(attempt_rows) == 1, "No attempt rows written"

        latest = attempt_rows[0]
        # Lavfi fixture has no poker content. Gemini's response is stochastic:
        #   - complete_uncontested: hand judged uncontested (the likely answer)
        #   - complete: a spurious detection (unlikely, but legitimate)
        #   - failed_transient: no_first_voluntary_commitment_found / no_second_action_found
        # All prove the orchestration chain ran end-to-end correctly.
        assert latest.status in (
            "complete", "complete_uncontested", "complete_skipped", "failed_transient"
        ), (
            f"Unexpected status {latest.status!r}: {latest.status_message}"
        )

        # Atomicity invariant, asserted per status. Each terminal success now
        # names its own row count, so this can no longer pass for the wrong
        # reason the way a single "not complete => zero rows" check did.
        hand_start_count = list(bq_client.query(
            f"SELECT COUNT(*) AS n FROM `{hand_starts_ref}` WHERE hand_setup_id = @hand_setup_id",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("hand_setup_id", "STRING", hand_setup_id)]
            ),
        ).result())[0].n

        # failed_* is deliberately unconstrained: the outage path described in
        # ARCHITECTURE's "Idempotent stage writes" legitimately leaves a row
        # under a failed status.
        if latest.status == "complete":
            assert hand_start_count == 1, (
                f"Atomicity violation: status=complete but {hand_start_count} hand_starts rows exist"
            )
        elif latest.status in ("complete_uncontested", "complete_skipped"):
            assert hand_start_count == 0, (
                f"Atomicity violation: status={latest.status} but {hand_start_count} hand_starts rows exist"
            )

    finally:
        # Cleanup in reverse dependency order
        for table, col, val in [
            (hand_starts_ref, "hand_setup_id", hand_setup_id),
            (attempts_ref, "hand_setup_id", hand_setup_id),
            (hand_setups_ref, "hand_setup_id", hand_setup_id),
            (clip_ref, "clip_id", clip_id),
            (videos_ref, "video_id", video_id),
        ]:
            bq_client.query(
                f"DELETE FROM `{table}` WHERE {col} = @val",
                job_config=bq.QueryJobConfig(
                    query_parameters=[bq.ScalarQueryParameter("val", "STRING", val)]
                ),
            ).result()

        # Delete test video from GCS
        if video_blob.exists():
            video_blob.delete()

        # Delete any frame objects from the hand_starts bucket
        for blob in gcs_client.bucket(hand_starts_bucket).list_blobs(prefix=f"{video_id}/"):
            blob.delete()


# ---------------------------------------------------------------------------
# _find_pending_hand_setups — pending-query integration tests (consecutive
# failure counting, retry cap)
# ---------------------------------------------------------------------------

_PENDING_QUERY_PROJECT = "table-talk-497020"
_PENDING_QUERY_DATASET = "table_talk_dev"


def _seed_hand_setup_for_pending_query(bq_client):
    from table_talk._generated.hand_setups_row import HandSetupsRow
    from table_talk.hand_setups_writer import write_hand_setups
    from table_talk.videos_writer import VideosRow, write_video_row

    uid = uuid.uuid4().hex[:10]
    video_id = f"test_p4q_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"

    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Pending query test video",
            duration_seconds=60,
            gcs_path=f"gs://fake-bucket/{video_id}.mp4",
            file_size_bytes=1,
        ),
        project=_PENDING_QUERY_PROJECT,
        dataset=_PENDING_QUERY_DATASET,
        client=bq_client,
    )
    write_hand_setups(
        [
            HandSetupsRow(
                hand_setup_id=hand_setup_id,
                clip_id=clip_id,
                video_id=video_id,
                hand_setup_time_seconds=0,
                frame_gcs_path=f"gs://fake-bucket/{hand_setup_id}.jpg",
                hand_setup_state={"players": []},
            )
        ],
        clip_id=clip_id,
        project_id=_PENDING_QUERY_PROJECT,
        dataset=_PENDING_QUERY_DATASET,
        client=bq_client,
    )
    return video_id, hand_setup_id


def _write_pending_query_attempt(bq_client, hand_setup_id, status):
    from table_talk._generated.hand_setup_processing_attempts_row import HandSetupProcessingAttemptsRow
    from table_talk.hand_setup_processing_attempts_writer import write_hand_setup_processing_attempt_row

    write_hand_setup_processing_attempt_row(
        HandSetupProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_setup_id=hand_setup_id,
            status=status,
            status_message=status,
        ),
        project=_PENDING_QUERY_PROJECT,
        dataset=_PENDING_QUERY_DATASET,
        client=bq_client,
    )


def _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id):
    from google.cloud import bigquery as bq

    videos_ref = f"{_PENDING_QUERY_PROJECT}.{_PENDING_QUERY_DATASET}.videos"
    hand_setups_ref = f"{_PENDING_QUERY_PROJECT}.{_PENDING_QUERY_DATASET}.hand_setups"
    attempts_ref = f"{_PENDING_QUERY_PROJECT}.{_PENDING_QUERY_DATASET}.hand_setup_processing_attempts"
    for table, col, val in [
        (attempts_ref, "hand_setup_id", hand_setup_id),
        (hand_setups_ref, "hand_setup_id", hand_setup_id),
        (videos_ref, "video_id", video_id),
    ]:
        bq_client.query(
            f"DELETE FROM `{table}` WHERE {col} = @val",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("val", "STRING", val)]
            ),
        ).result()


@pytest.mark.integration
def test_find_pending_hand_setups_transient_then_complete_then_transient_selected_count_one():
    """The reset case: failed_transient -> complete -> failed_transient must
    report consecutive_failures=1, not the lifetime count of 2."""
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")
        _write_pending_query_attempt(bq_client, hand_setup_id, "complete")
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert len(results) == 1, f"Expected entity to be selected, got {results}"
        assert results[0].consecutive_failures == 1, (
            f"Expected consecutive_failures=1 (reset case), got {results[0].consecutive_failures}"
        )
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


@pytest.mark.integration
def test_find_pending_hand_setups_no_attempts_selected_zero_count():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert len(results) == 1
        assert results[0].consecutive_failures == 0
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


@pytest.mark.integration
def test_find_pending_hand_setups_three_transient_selected_count_three():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        for _ in range(3):
            _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert len(results) == 1
        assert results[0].consecutive_failures == 3
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


@pytest.mark.integration
def test_find_pending_hand_setups_complete_then_transient_then_complete_not_selected():
    """Mirrors the real MPBLfM4mwfE_006_004 shape: a failure sandwiched
    between two successes must not carry forward and must not park the
    entity — the latest status is 'complete', so it's excluded entirely."""
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        _write_pending_query_attempt(bq_client, hand_setup_id, "complete")
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")
        _write_pending_query_attempt(bq_client, hand_setup_id, "complete")

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert results == [], f"Expected entity to be excluded, got {results}"
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


@pytest.mark.integration
def test_find_pending_hand_setups_latest_complete_uncontested_not_selected():
    """complete_uncontested is a terminal success and must never be re-selected.

    The regression this guards: if the pending query ever deny-listed
    'complete' literally instead of allow-listing 'failed_transient', an
    uncontested hand would be permanently pending and would burn a step-A
    Gemini call on every run for a hand that by definition can never produce
    a hand_starts row.
    """
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        _write_pending_query_attempt(bq_client, hand_setup_id, "complete_uncontested")

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert results == [], f"Expected entity to be excluded, got {results}"
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


@pytest.mark.integration
def test_find_pending_hand_setups_latest_parked_not_selected():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)
    try:
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_transient")
        _write_pending_query_attempt(bq_client, hand_setup_id, "failed_parked")

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )
        assert results == [], f"Expected parked entity to be excluded, got {results}"
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)
