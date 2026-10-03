import asyncio
import os
import subprocess
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from table_talk.gemini_caller import (
    CLIP_MEDIA_RESOLUTION,
    FRAME_RESOLUTION_ULTRA_HIGH,
    GeminiPermanentError,
    GeminiTransientError,
)
from table_talk.mark_pending import MARK_MESSAGE_PREFIX, MARK_STATUS
from table_talk.hand_start_processing import (
    PendingHandSetup,
    _find_pending_hand_setups,
    _hallucination_guard,
    _transient_status,
    check_duplicate_hole_cards,
    check_fva,
    check_fva_amount,
    check_missing_hole_cards,
    check_preconditions,
    eligible_seats_at_fva,
    process_hand_setup,
    process_pending_hand_setups,
)
from table_talk.videos_downloader import DownloadPermanentError

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _hand_setup_state(players=None, total_seat_count=2, pot_size_bb=1.5):
    if players is None:
        players = [
            {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
            {"seat_position_label": "BTN", "stack_size": 50.0, "seat_number": 3},
        ]
    return {
        "total_seat_count": total_seat_count,
        "pot_size_bb": pot_size_bb,
        "players": players,
    }


_P4_HASHES = {
    "prompts/identify_hand_start.md": "dddddddddddd",
    "prompts/extract_hole_cards.md": "eeeeeeeeeeee",
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
        bounty_type="none",
)

_CLIP_RESULT_FOUND = {
    "found": True,
    "timestamp": "01:45",  # 105s, within [100, 160]
    "second_action_timestamp": "01:50",  # 110s
    "seat_position_label": "BTN",
    "action_type": "raise",
    "bet_amount": 3.0,
}

_CLIP_RESULT_FVA_SB = {
    "found": True,
    "timestamp": "01:45",
    "second_action_timestamp": "01:50",
    "seat_position_label": "SB",  # seat_number 2 — BTN (seat 3) is non-eligible
    "action_type": "raise",
    "bet_amount": 3.0,
}

_HOLE_CARDS_RESULT = {
    "players": [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
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
    assert check_preconditions(_hand_setup_state(), "none") is None


def test_check_preconditions_null_stack_size():
    state = _hand_setup_state(players=[
        {"seat_position_label": "BB", "stack_size": None, "seat_number": 1},
    ])
    reason = check_preconditions(state, "none")
    assert reason is not None
    assert "null stack_size" in reason
    assert "BB" in reason


def test_check_preconditions_null_seat_position_label():
    state = _hand_setup_state(players=[
        {"seat_position_label": None, "stack_size": 75.0, "seat_number": 1},
    ])
    reason = check_preconditions(state, "none")
    assert reason is not None
    assert "null seat_position_label" in reason
    assert "75.0" in reason


def test_check_preconditions_both_null_reports_under_null_stack():
    """A player with both null stack and null label is reported by check 1."""
    state = _hand_setup_state(players=[
        {"seat_position_label": None, "stack_size": None, "seat_number": 1},
    ])
    reason = check_preconditions(state, "none")
    assert reason is not None
    assert "null stack_size" in reason


def test_check_preconditions_total_seat_count_too_low():
    state = _hand_setup_state(total_seat_count=1)
    reason = check_preconditions(state, "none")
    assert reason is not None
    assert "total_seat_count" in reason


def test_check_preconditions_total_seat_count_null():
    state = _hand_setup_state(total_seat_count=None)
    reason = check_preconditions(state, "none")
    assert reason is not None
    assert "total_seat_count" in reason


def test_check_preconditions_pot_size_bb_zero_or_null():
    for bad_pot in (0, None):
        state = _hand_setup_state(pot_size_bb=bad_pot)
        reason = check_preconditions(state, "none")
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
    assert (
        "a.latest_status IS NULL\n"
        "               OR a.latest_status IN ('failed_transient', 'marked_pending')"
    ) in query
    assert "only_video_ids" not in query
    assert "only_hand_setup_ids" not in query

    job_config = mock_client.query.call_args[1]["job_config"]
    param_names = {p.name for p in job_config.query_parameters}
    assert param_names == {"max_available_seconds", "mark_message_prefix"}


def test_find_pending_hand_setups_treats_a_mark_as_a_non_failure():
    """A mark resets the count rather than spending a retry slot, including the
    pre-'marked_pending' marks already in the table."""
    mock_client = _mock_bq_client()
    _find_pending_hand_setups("proj", "ds", client=mock_client)

    query = mock_client.query.call_args[0][0]
    assert "OR status_message LIKE CONCAT(@mark_message_prefix, '%')" in query
    params = {
        p.name: p.value for p in mock_client.query.call_args[1]["job_config"].query_parameters
    }
    assert params["mark_message_prefix"] == MARK_MESSAGE_PREFIX


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
    row.bounty_type = "none"
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
        bounty_type="none",
    )


# ---------------------------------------------------------------------------
# process_hand_setup — happy path
# ---------------------------------------------------------------------------


def test_step_a_clip_call_uses_the_hand_start_clip_model():
    # Phase 4 keeps Flash, on its own variable rather than one shared with the
    # phase that needs Pro.
    from table_talk.gemini_caller import HAND_START_CLIP_MODEL

    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND) as clip,
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
        patch(
            "table_talk.hand_start_processing.HAND_START_CLIP_MODEL",
            "sentinel-hand-start-model",
        ),
    ):
        _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    # Sentinel rather than the literal: the three Flash defaults coincide, so
    # asserting "gemini-3.8-flash" would pass if this read FRAME_MODEL instead.
    assert clip.call_args.kwargs["model"] == "sentinel-hand-start-model"
    assert HAND_START_CLIP_MODEL == "gemini-3.8-flash"


def test_gemini_calls_are_tagged_with_the_hand_setup_id():
    """Step A's clip call and both step C frame reads name the hand setup.

    The step C retry reuses the same frame and prompt, so without the tag its
    cost is indistinguishable from the first read's on the usage line.
    """
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND) as clip,
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT) as frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert clip.call_args.kwargs["entity_id"] == "clip_001_001"
    assert {c.kwargs["entity_id"] for c in frame.call_args_list} == {"clip_001_001"}


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
            prompt_hashes=_P4_HASHES,
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
    assert by_label["BTN"]["hole_cards"] == ["2c", "3c"]

    fva = row.hand_start_state["fva"]
    assert fva["seat_position_label"] == "BTN"
    assert fva["seat_number"] == 3

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
        bounty_type="none",
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
            prompt_hashes=_P4_HASHES,
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
        hand_setup_state=_hand_setup_state(total_seat_count=3, players=[
            {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
            {"seat_position_label": "SB", "stack_size": 60.0, "seat_number": 2},
            {"seat_position_label": "BTN", "stack_size": 50.0, "seat_number": 3},
        ]),
        available_seconds=60,
        raw_lead_gap_seconds=60,
        consecutive_failures=0,
        bounty_type="none",
    )
    # Gemini's response omits BTN — its hole_cards should end up None. The FVA
    # is SB, so BTN is non-eligible and neither hole-card gate fires: this test
    # is about the matching loop, not about whether a null is tolerated.
    with (
        patch(
            "table_talk.hand_start_processing.call_gemini_for_clip",
            return_value=_CLIP_RESULT_FVA_SB,
        ),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            return_value={"players": [
                {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
                {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
            ]},
        ),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BTN"]["hole_cards"] is None
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
        hand_setup_state=_hand_setup_state(total_seat_count=3, players=[
            {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
            {"seat_position_label": "SB", "stack_size": 60.0, "seat_number": 2},
            {"seat_position_label": "BTN", "stack_size": 50.0, "seat_number": 3},
        ]),
        available_seconds=60,
        raw_lead_gap_seconds=60,
        consecutive_failures=0,
        bounty_type="none",
    )


def test_step_c_reads_the_fva_frame_at_ultra_high_resolution():
    """Both step C calls, the first read and the gap-fill retry.

    Suit misreads on face cards are a resolution problem, not a prompt problem:
    measured over the four known misreads, the baseline missed 4 of 160 cards
    and a prompt instruction moved nothing, while ULTRA_HIGH read 0 of 640. Set
    per call site, so Phase 3's player info and the payout panel read are
    unaffected. Reverting this silently restores the misreads.
    """
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
            # SB omitted -> null on the first call, so the retry fires too.
        ]
    }
    second_response = {
        "players": [{"seat_position_label": "SB", "hole_cards": ["Th", "9h"]}]
    }
    with (
        patch(
            "table_talk.hand_start_processing.call_gemini_for_clip",
            return_value=_CLIP_RESULT_FOUND,
        ),
        patch(
            "table_talk.hand_start_processing.extract_frame",
            side_effect=_fake_extract_frame,
        ),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            side_effect=[first_response, second_response],
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert mock_gemini_frame.call_count == 2
    for call_args in mock_gemini_frame.call_args_list:
        assert call_args.kwargs["frame_media_resolution"] == FRAME_RESOLUTION_ULTRA_HIGH


def test_process_hand_setup_retry_fills_eligible_null():
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
            # SB omitted -> null on first call
        ]
    }
    second_response = {
        "players": [
            {"seat_position_label": "SB", "hole_cards": ["Th", "9h"]},
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
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 2
    # Retry must reuse the exact same prompt/frame/args as the first call.
    assert mock_gemini_frame.call_args_list[0] == mock_gemini_frame.call_args_list[1]

    mock_write_starts.assert_called_once()
    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]
    assert by_label["SB"]["hole_cards"] == ["Th", "9h"]
    assert by_label["BTN"]["hole_cards"] == ["2c", "3c"]

    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "complete"
    assert "null hole_cards" not in attempt_row.status_message


def test_process_hand_setup_retry_still_null_on_the_fva_seat_is_failed_permanent():
    """P4-6, on the one seat it still judges.

    The FVA is BTN and BTN is the seat that will not read. A null there has
    already survived a second read of the same frame, and all three observed
    causes are properties of that frame, so a further attempt reads the same
    pixels. The accepted cost is that a hand another verification frame could
    have resolved is lost.
    """
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
        ]
    }
    second_response = {"players": []}  # retry also misses BTN
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
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "failed_permanent"
    # The retry still ran — P4-6 fires after it, not instead of it.
    assert mock_gemini_frame.call_count == 2
    # Failures never write a stage row.
    mock_write_starts.assert_not_called()

    attempt_row = mock_write_attempt.call_args[0][0]
    assert attempt_row.status == "failed_permanent"
    assert attempt_row.status_message.startswith("P4-6: missing_hole_cards_live_seat: ")
    assert "BTN" in attempt_row.status_message


def test_process_hand_setup_retry_still_null_off_the_fva_seat_completes():
    """The narrowing, at the orchestrator. The FVA is BTN and it reads; SB does
    not, and the hand completes with the null carried into hand_start_state for
    P5-16 to judge once step D says whether SB stayed in after the FVA.

    Two corpus hands this used to park had the unreadable seat fold at its first
    action after the FVA, so both still carried their whole story.
    """
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
        ]
    }
    second_response = {"players": []}  # retry also misses SB
    with (
        patch(
            "table_talk.hand_start_processing.call_gemini_for_clip",
            return_value=_CLIP_RESULT_FOUND,
        ),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            side_effect=[first_response, second_response],
        ) as mock_gemini_frame,
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch(
            "table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"
        ) as mock_write_attempt,
    ):
        outcome = _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    # The retry still ran: the read is attempted for every eligible seat, and
    # only the judging narrowed.
    assert mock_gemini_frame.call_count == 2

    mock_write_starts.assert_called_once()
    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["SB"]["hole_cards"] is None
    assert by_label["BTN"]["hole_cards"] == ["2c", "3c"]

    assert mock_write_attempt.call_args[0][0].status == "complete"


def test_process_hand_setup_retry_does_not_clobber_first_call_answer():
    hs = _three_seat_hs()
    first_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
            # SB omitted -> null, triggers retry
        ]
    }
    second_response = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": None},  # simulated flip: null this time
            {"seat_position_label": "SB", "hole_cards": ["Th", "9h"]},
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
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 2

    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BB"]["hole_cards"] == ["Ah", "Kd"]  # retained, not clobbered by retry's null
    assert by_label["SB"]["hole_cards"] == ["Th", "9h"]  # filled by retry
    assert by_label["BTN"]["hole_cards"] == ["2c", "3c"]


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
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 1

    attempt_row = mock_write_attempt.call_args[0][0]
    assert "null hole_cards" not in attempt_row.status_message


def test_process_hand_setup_non_eligible_null_does_not_trigger_retry():
    hs = _three_seat_hs()
    # FVA is SB (seat 2) -> BTN (seat 3) is non-eligible, and it's the one that's null.
    result = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
            # BTN omitted -> null, but non-eligible so no gate demands it
        ]
    }
    with (
        patch(
            "table_talk.hand_start_processing.call_gemini_for_clip",
            return_value=_CLIP_RESULT_FVA_SB,
        ),
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
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "complete"
    assert mock_gemini_frame.call_count == 1

    players = mock_write_starts.call_args[0][0][0].hand_start_state["hand_setup"]["players"]
    by_label = {p["seat_position_label"]: p for p in players}
    assert by_label["BTN"]["hole_cards"] is None

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
        bounty_type="none",
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0, "none"),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60, 0, "none"),
        PendingHandSetup("vid_b_001_001", "vid_b_001", "vid_b", 0, {}, 60, 60, 0, "none"),
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video") as mock_download,
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock, return_value="complete") as mock_process,
    ):
        stats = _run(process_pending_hand_setups(
            "proj", "ds", "vbucket", "hbucket", "id_prompt", "eh_prompt",
            prompt_hashes=_P4_HASHES,
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
            prompt_hashes=_P4_HASHES,
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
        _run(process_pending_hand_setups(
            "proj", "ds", "vb", "hb", "ip", "ep", prompt_hashes=_P4_HASHES,
        ))

    mock_find.assert_called_once_with(
        "proj", "ds",
        only_video_ids=None,
        only_hand_setup_ids=None,
        client=None,
    )


def test_process_pending_hand_setups_download_failure_marks_transient():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0, "none")
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=Exception("network error")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups(
            "proj", "ds", "vb", "hb", "ip", "ep", prompt_hashes=_P4_HASHES,
        ))

    mock_process.assert_not_called()
    assert stats["hand_setups_processed"] == 1
    assert stats["hand_setups_failed_transient"] == 1
    assert stats["hand_setups_failed_parked"] == 0
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_transient"
    assert "video_download_failed" in attempt_row.status_message


def test_process_pending_hand_setups_download_failure_parks_at_cap():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 2, "none")
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=Exception("network error")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups(
            "proj", "ds", "vb", "hb", "ip", "ep",
            prompt_hashes=_P4_HASHES, max_attempts=3,
        ))

    mock_process.assert_not_called()
    assert stats["hand_setups_failed_parked"] == 1
    attempt_row = mock_attempt.call_args[0][0]
    assert attempt_row.status == "failed_parked"


def test_process_pending_hand_setups_download_not_found_marks_permanent():
    hand_setups = [
        PendingHandSetup("vid_a_001_001", "vid_a_001", "vid_a", 0, {}, 60, 60, 0, "none"),
        PendingHandSetup("vid_a_001_002", "vid_a_001", "vid_a", 60, {}, 60, 60, 0, "none"),
    ]
    with (
        patch("table_talk.hand_start_processing._find_pending_hand_setups", return_value=hand_setups),
        patch("table_talk.hand_start_processing.download_video", side_effect=DownloadPermanentError("gone")),
        patch("table_talk.hand_start_processing.process_hand_setup", new_callable=AsyncMock) as mock_process,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        stats = _run(process_pending_hand_setups(
            "proj", "ds", "vb", "hb", "ip", "ep", prompt_hashes=_P4_HASHES,
        ))

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
    from table_talk._generated.tournament_results_row import TournamentResultsRow
    from table_talk.clip_manifest_writer import ClipManifestRow as CMRow
    from table_talk.clip_manifest_writer import write_clip_manifest_rows
    from table_talk.hand_setups_writer import write_hand_setups
    from table_talk.tournament_results_writer import write_tournament_results
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
    results_ref = f"{project}.{dataset}.tournament_results"

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
    # Required setup since Phase 4's pending query joins tournament_results for
    # bounty_type and raises on a null. bounty_type="none" keeps the hand on the
    # P4-2 branch, which the fixture's bounty-free players satisfy.
    write_tournament_results(
        [
            TournamentResultsRow(
                video_id=video_id,
                bounty_type="none",
                currency_symbol="$",
                frame_timestamp_seconds=0,
                frame_gcs_path=f"gs://{videos_bucket}/{video_id}/results.jpg",
                tournament_results_state={"panel": {"rows": []}},
            )
        ],
        video_id=video_id,
        project_id=project,
        dataset=dataset,
        client=bq_client,
    )
    write_clip_manifest_rows(
        [CMRow(clip_id=clip_id, video_id=video_id, clip_start_time=0, clip_end_time=60)],
        video_id=video_id,
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
                    "total_seat_count": 2,
                    "pot_size_bb": 1.5,
                    "players": [
                        {"seat_position_label": "BB", "stack_size": 100.0, "seat_number": 1},
                        {"seat_position_label": "BTN", "stack_size": 100.0, "seat_number": 3},
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
            prompt_hashes=_P4_HASHES,
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
            (results_ref, "video_id", video_id),
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
    from table_talk._generated.tournament_results_row import TournamentResultsRow
    from table_talk.hand_setups_writer import write_hand_setups
    from table_talk.tournament_results_writer import write_tournament_results
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
    # Phase 4's pending query joins tournament_results for bounty_type and
    # raises on a null, so this row is now required setup rather than optional
    # context. Written with the production writer, per CLAUDE.md's rule that
    # cross-phase test setup goes through writers and never orchestrators.
    write_tournament_results(
        [
            TournamentResultsRow(
                video_id=video_id,
                bounty_type="none",
                currency_symbol="$",
                frame_timestamp_seconds=0,
                frame_gcs_path=f"gs://fake-bucket/{video_id}/results.jpg",
                tournament_results_state={"panel": {"rows": []}},
            )
        ],
        video_id=video_id,
        project_id=_PENDING_QUERY_PROJECT,
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


def _write_pending_query_attempt(bq_client, hand_setup_id, status, status_message=None):
    from table_talk._generated.hand_setup_processing_attempts_row import (
        HandSetupProcessingAttemptsRow,
    )
    from table_talk.hand_setup_processing_attempts_writer import (
        write_hand_setup_processing_attempt_row,
    )

    write_hand_setup_processing_attempt_row(
        HandSetupProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_setup_id=hand_setup_id,
            status=status,
            status_message=status if status_message is None else status_message,
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
    results_ref = f"{_PENDING_QUERY_PROJECT}.{_PENDING_QUERY_DATASET}.tournament_results"
    # Reverse dependency order: tournament_results hangs off videos, so it goes
    # before the video row and after anything keyed on the hand setup.
    for table, col, val in [
        (attempts_ref, "hand_setup_id", hand_setup_id),
        (hand_setups_ref, "hand_setup_id", hand_setup_id),
        (results_ref, "video_id", video_id),
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


# A mark must leave the entity eligible with a clean retry budget whatever
# preceded it, and marking twice must cost nothing. Written through
# mark_pending's own status and message, so a rename of either moves these
# tests with it rather than leaving them asserting a stale literal.
_PENDING_MARK = (MARK_STATUS, f"{MARK_MESSAGE_PREFIX}hand_starts")
# A mark written before `marked_pending` existed: a `failed_transient` row
# carrying the mark message. Over a thousand are in the corpus, and the counter
# has to recognise them by message or they keep spending a retry slot.
_PENDING_OLD_MARK = ("failed_transient", f"{MARK_MESSAGE_PREFIX}hand_starts")

_MARK_BUDGET_HISTORIES = [
    pytest.param(
        ["failed_transient", "failed_transient", "failed_transient", "failed_parked",
         _PENDING_MARK],
        0, id="parked_then_mark",
    ),
    pytest.param([_PENDING_MARK, _PENDING_MARK], 0, id="marked_twice"),
    pytest.param([_PENDING_MARK, "failed_transient"], 1, id="mark_then_one_real_failure"),
    pytest.param(
        ["failed_transient", "failed_transient", _PENDING_OLD_MARK], 0, id="historical_mark"
    ),
    pytest.param(["complete", _PENDING_MARK], 0, id="success_then_mark"),
]


@pytest.mark.integration
@pytest.mark.parametrize("history,expected_failures", _MARK_BUDGET_HISTORIES)
def test_find_pending_hand_setups_gives_a_marked_setup_a_clean_retry_budget(
    history, expected_failures
):
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_PENDING_QUERY_PROJECT)
    video_id, hand_setup_id = _seed_hand_setup_for_pending_query(bq_client)

    try:
        for entry in history:
            status, message = entry if isinstance(entry, tuple) else (entry, None)
            _write_pending_query_attempt(bq_client, hand_setup_id, status, message)

        results = _find_pending_hand_setups(
            _PENDING_QUERY_PROJECT, _PENDING_QUERY_DATASET,
            only_hand_setup_ids=[hand_setup_id], client=bq_client,
        )

        assert len(results) == 1, f"Expected the marked setup to be selected, got {results}"
        assert results[0].consecutive_failures == expected_failures
    finally:
        _cleanup_pending_query_fixture(bq_client, video_id, hand_setup_id)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_is_a_sibling_and_lists_both_prompts():
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    state = mock_write_starts.call_args[0][0][0].hand_start_state
    assert set(state) == {"hand_setup", "fva", "provenance"}
    assert set(state["provenance"]["prompts"]) == {
        "prompts/identify_hand_start.md",
        "prompts/extract_hole_cards.md",
    }


def test_provenance_records_the_step_c_read_resolution():
    """Step C reads at ULTRA_HIGH, and the row has to say so.

    The change to ULTRA_HIGH altered extraction behaviour without touching the
    model id or either prompt hash, so rows either side of it were previously
    byte-identical in provenance. This is what tells them apart.
    """
    hs = _three_seat_hs()
    frame_result = {
        "players": [
            {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
            {"seat_position_label": "SB", "hole_cards": ["Th", "9h"]},
            {"seat_position_label": "BTN", "hole_cards": ["2c", "3c"]},
        ]
    }
    with (
        patch(
            "table_talk.hand_start_processing.call_gemini_for_clip",
            return_value=_CLIP_RESULT_FOUND,
        ),
        patch(
            "table_talk.hand_start_processing.extract_frame",
            side_effect=_fake_extract_frame,
        ),
        patch(
            "table_talk.hand_start_processing.call_gemini_for_frame",
            return_value=frame_result,
        ),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    provenance = mock_write_starts.call_args[0][0][0].hand_start_state["provenance"]
    assert provenance["media_resolution"] == {
        "clip": CLIP_MEDIA_RESOLUTION,
        "frame": FRAME_RESOLUTION_ULTRA_HIGH,
    }
    assert set(provenance["media_resolution"]) == set(provenance["models"])


def test_phase_3_provenance_rides_through_the_nested_hand_setup():
    """Phase 4 nests hand_setup verbatim, so a Phase 3 provenance block reaches
    hand_starts with no code here assembling it. That chain is the design — a
    hand_actions row ends up carrying all three layers."""
    hs = replace(
        _HS,
        hand_setup_state={
            **_HS.hand_setup_state,
            "provenance": {
                "models": {"clip": "p3-model"},
                "prompts": {"prompts/identify_hand.md": "abc"},
            },
        },
    )
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=_HOLE_CARDS_RESULT),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row"),
    ):
        _run(process_hand_setup(
            hs, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    state = mock_write_starts.call_args[0][0][0].hand_start_state
    assert state["hand_setup"]["provenance"]["models"] == {"clip": "p3-model"}
    assert state["provenance"] != state["hand_setup"]["provenance"]


# ---------------------------------------------------------------------------
# P4-1 .. P4-3 — preconditions (no LLM call, complete_skipped)
# ---------------------------------------------------------------------------


def _bounty_state(bounties, total_seat_count=3):
    """A valid 3-handed hand with the given bounty per seat, in seat order."""
    labels = ["BB", "SB", "BTN"]
    players = []
    for i, (label, bounty) in enumerate(zip(labels, bounties), start=1):
        player = {"seat_position_label": label, "stack_size": 50.0, "seat_number": i}
        if bounty is not _ABSENT:
            player["bounty"] = bounty
        players.append(player)
    return _hand_setup_state(total_seat_count=total_seat_count, players=players)


_ABSENT = object()


def test_p4_1_progressive_video_with_every_bounty_present_passes():
    assert check_preconditions(_bounty_state([125.0, 250.0, 500.0]), "progressive") is None


@pytest.mark.parametrize("bounties,expected_seat", [
    ([125.0, None, 500.0], "SB"),
    ([_ABSENT, 250.0, 500.0], "BB"),
    ([125.0, 250.0, 0], "BTN"),          # zero is not a live bounty
])
def test_p4_1_progressive_video_missing_a_bounty_skips(bounties, expected_seat):
    reason = check_preconditions(_bounty_state(bounties), "progressive")
    assert reason.startswith("skipped: P4-1: null_bounty_progressive: ")
    assert expected_seat in reason


def test_p4_1_catches_the_phantom_seat_shape_the_null_stack_check_misses():
    """The documented non-null phantom carried a real-looking stack and a null
    bounty, so it passed the null-stack precondition and was processed."""
    state = _bounty_state([125.0, 250.0, 500.0], total_seat_count=4)
    state["players"].append(
        {"seat_position_label": "CO", "stack_size": 0.38, "seat_number": 4, "bounty": None}
    )
    reason = check_preconditions(state, "progressive")
    assert reason.startswith("skipped: P4-1: null_bounty_progressive: ")
    assert "CO" in reason


def test_p4_2_non_bounty_video_with_no_bounties_passes():
    assert check_preconditions(_bounty_state([_ABSENT] * 3), "none") is None


def test_p4_2_bounty_on_a_non_bounty_video_skips():
    """A value here means bounty_type was misclassified by the video's one
    payout read, which a retry of this hand cannot change."""
    reason = check_preconditions(_bounty_state([_ABSENT, 250.0, _ABSENT]), "none")
    assert reason.startswith("skipped: P4-2: bounty_on_non_bounty_video: ")
    assert "SB" in reason


@pytest.mark.parametrize("size,labels", [
    (2, ["BB", "BTN"]),
    (3, ["BB", "SB", "BTN"]),
    (6, ["BB", "SB", "BTN", "CO", "HJ", "LJ"]),
    (9, ["BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1", "UTG"]),
])
def test_p4_3_canonical_label_sets_pass(size, labels):
    state = _hand_setup_state(
        total_seat_count=size,
        players=[
            {"seat_position_label": lb, "stack_size": 50.0, "seat_number": i}
            for i, lb in enumerate(labels, start=1)
        ],
    )
    assert check_preconditions(state, "none") is None


@pytest.mark.parametrize("size,labels,why", [
    (3, ["BB", "SB"], "missing a seat"),
    (3, ["BB", "SB", "SB"], "repeated label"),
    (3, ["BB", "SB", "CO"], "foreign label for this size"),
    (2, ["BB", "SB"], "heads-up stores BTN, not SB"),
    (7, ["BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG"], "7-handed ends at UTG+2"),
])
def test_p4_3_invalid_label_sets_skip(size, labels, why):
    state = _hand_setup_state(
        total_seat_count=size,
        players=[
            {"seat_position_label": lb, "stack_size": 50.0, "seat_number": i}
            for i, lb in enumerate(labels, start=1)
        ],
    )
    reason = check_preconditions(state, "none")
    assert reason is not None, why
    assert reason.startswith("skipped: P4-3: invalid_label_set: "), why


def test_preconditions_first_failure_wins_and_null_stack_still_leads():
    """Ordering is load-bearing: the pre-existing checks run before the new
    ones, so a phantom seat reading null for everything still reports as a null
    stack rather than as a bounty gap."""
    state = _hand_setup_state(
        total_seat_count=3,
        players=[
            {"seat_position_label": "BB", "stack_size": None, "seat_number": 1},
            {"seat_position_label": "SB", "stack_size": 50.0, "seat_number": 2},
            {"seat_position_label": "BTN", "stack_size": 50.0, "seat_number": 3},
        ],
    )
    assert "null stack_size" in check_preconditions(state, "progressive")


# ---------------------------------------------------------------------------
# P4-4 .. P4-7 — output checks
# ---------------------------------------------------------------------------


_THREE_SEATS = {
    "players": [
        {"seat_position_label": "BB", "seat_number": 1},
        {"seat_position_label": "SB", "seat_number": 2},
        {"seat_position_label": "BTN", "seat_number": 3},
    ]
}


@pytest.mark.parametrize("action_type", ["call", "raise", "all_in"])
def test_p4_4_valid_fva_action_types_pass(action_type):
    fva = {"seat_position_label": "BTN", "action_type": action_type, "seat_number": 3}
    assert check_fva(fva, _THREE_SEATS) is None


@pytest.mark.parametrize("action_type", ["fold", "check", "bet", "limp", None])
def test_p4_4_non_commitment_action_types_fail(action_type):
    """The FVA is the first voluntary chip commitment; fold and check commit
    nothing, and bet is not in identify_hand_start.md's vocabulary."""
    fva = {"seat_position_label": "BTN", "action_type": action_type, "seat_number": 3}
    reason = check_fva(fva, _THREE_SEATS)
    assert reason.startswith("P4-4: invalid_fva_action_type: ")


def test_p4_4_fva_label_not_in_the_hand_fails():
    """An unresolvable label leaves seat_number None, which the eligible-seat
    calculation reads as 'every seat' — so this gate is what stops a bad label
    silently widening the hole-card requirement to the whole table."""
    fva = {"seat_position_label": "UTG", "action_type": "raise", "seat_number": None}
    reason = check_fva(fva, _THREE_SEATS)
    assert reason.startswith("P4-4: invalid_fva_action_type: ")
    assert "not a seat in this hand" in reason


def test_p4_5_distinct_cards_pass():
    players = [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
    ]
    assert check_duplicate_hole_cards(players) is None


def test_p4_5_duplicate_across_two_seats_fails():
    players = [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "SB", "hole_cards": ["Ah", "3c"]},
    ]
    reason = check_duplicate_hole_cards(players)
    assert reason.startswith("P4-5: duplicate_hole_card: ")
    assert "BB" in reason and "SB" in reason


def test_p4_5_duplicate_within_one_seat_fails():
    players = [{"seat_position_label": "BB", "hole_cards": ["Ah", "Ah"]}]
    assert check_duplicate_hole_cards(players).startswith("P4-5: duplicate_hole_card: ")


def test_p4_5_compares_case_folded():
    """normalize_card only maps '10' -> 'T'; it does not canonicalize case, so a
    naive comparison would let this through."""
    players = [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "SB", "hole_cards": ["AH", "3c"]},
    ]
    assert check_duplicate_hole_cards(players) is not None


def test_p4_5_ignores_nulls():
    """A null is P4-6's or P5-16's business, not P4-5's — two nulls are not a
    duplicate."""
    players = [
        {"seat_position_label": "BB", "hole_cards": [None, None]},
        {"seat_position_label": "SB", "hole_cards": [None, "3c"]},
    ]
    assert check_duplicate_hole_cards(players) is None


def test_p4_6_all_eligible_seats_readable_passes():
    players = [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
    ]
    assert check_missing_hole_cards(players, "SB") is None


@pytest.mark.parametrize("cards", [None, [], [None, None], ["Ah", None]])
def test_p4_6_an_unreadable_card_on_the_fva_seat_fails(cards):
    """The FVA seat committed chips, so it stayed in by construction — H5's
    live-seat test needs no actions for this one seat."""
    players = [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "SB", "hole_cards": cards},
    ]
    reason = check_missing_hole_cards(players, "SB")
    assert reason.startswith("P4-6: missing_hole_cards_live_seat: ")
    assert "SB" in reason


@pytest.mark.parametrize("cards", [None, [], [None, None], ["Ah", None]])
def test_p4_6_ignores_an_unreadable_card_off_the_fva_seat(cards):
    """The narrowing. Whether a non-FVA seat stayed in after the FVA depends on
    actions that do not exist until step D has run, so P5-16 judges it — and two
    hands this used to park had the unreadable seat fold at its first action."""
    players = [
        {"seat_position_label": "BB", "hole_cards": cards},
        {"seat_position_label": "SB", "hole_cards": ["2c", "3c"]},
    ]
    assert check_missing_hole_cards(players, "SB") is None


_SEAT_NUMBERS = {"BB": 1, "SB": 2, "BTN": 3, "CO": 4, "HJ": 5, "LJ": 6}


def _stacked(total_seat_count: int, stacks: dict[str, float]) -> dict:
    """A hand setup carrying stacks and seat numbers. Seat numbers are what
    posted_blind_for reads, since the blinds go by role and not by label."""
    return {
        "total_seat_count": total_seat_count,
        "pot_size_bb": 1.5,
        "players": [
            {"seat_position_label": label, "seat_number": _SEAT_NUMBERS[label],
             "stack_size": stack}
            for label, stack in stacks.items()
        ],
    }


def _fva_amount(label: str, action_type: str, bet_amount) -> dict:
    return {
        "seat_position_label": label, "seat_number": _SEAT_NUMBERS[label],
        "action_type": action_type, "bet_amount": bet_amount,
    }


@pytest.mark.parametrize("seats,stacks,label,amount,hits", [
    # YzKyFMQ1avU_014_003 as recorded, and corrected. 17.1 is the stack *after*
    # the 0.5 blind, so the total in front is 17.6 — what step D read.
    (6, {"BB": 20.0, "SB": 17.1, "BTN": 9.0, "CO": 15.0, "HJ": 11.0, "LJ": 8.0},
     "SB", 17.1, True),
    (6, {"BB": 20.0, "SB": 17.1, "BTN": 9.0, "CO": 15.0, "HJ": 11.0, "LJ": 8.0},
     "SB", 17.6, False),
    # Heads-up, where the BTN posts the small blind and normalize_heads_up has
    # left no seat labelled SB at all. A label-based blind lookup charges the BTN
    # nothing and would wave through the 20.0 — half a blind short.
    (2, {"BB": 30.0, "BTN": 20.0}, "BTN", 20.5, False),
    (2, {"BB": 30.0, "BTN": 20.0}, "BTN", 20.0, True),
    (2, {"BB": 20.0, "BTN": 30.0}, "BB", 21.0, False),
    (2, {"BB": 20.0, "BTN": 30.0}, "BB", 20.0, True),
    # A seat that posted nothing: the whole stack is the whole amount.
    (6, {"BB": 20.0, "SB": 10.0, "BTN": 9.0, "CO": 15.0, "HJ": 11.0, "LJ": 8.0},
     "CO", 15.0, False),
])
def test_p4_7_an_all_in_must_equal_the_stack_plus_the_blind_posted_by_role(
    seats, stacks, label, amount, hits
):
    reason = check_fva_amount(_fva_amount(label, "all_in", amount),
                              _stacked(seats, stacks))
    if hits:
        assert reason.startswith("P4-7: fva_amount_mismatch: ")
        assert label in reason and "behind" in reason
    else:
        assert reason is None


@pytest.mark.parametrize("action_type", ["call", "raise", "all_in"])
def test_p4_7_no_fva_may_commit_more_than_the_stack_plus_its_blind(action_type):
    """The over-commitment shape, which is a different defect from being wrongly
    labelled all-in and gets its own message — the same split P5-5 makes."""
    reason = check_fva_amount(
        _fva_amount("SB", action_type, 20.0),
        _stacked(3, {"BB": 20.0, "SB": 17.1, "BTN": 9.0}),
    )
    assert reason.startswith("P4-7: fva_amount_mismatch: ")
    assert "beyond its stack" in reason


@pytest.mark.parametrize("action_type", ["call", "raise"])
def test_p4_7_a_whole_stack_call_or_raise_is_accepted(action_type):
    """Forward only, consistent with P5-5: an FVA for the seat's whole stack need
    not be recorded all_in. YzKyFMQ1avU_013_003's shape, one phase earlier."""
    assert check_fva_amount(
        _fva_amount("BB", action_type, 8.03),
        _stacked(3, {"BB": 7.03, "SB": 20.0, "BTN": 15.0}),
    ) is None


def test_p4_7_tolerates_display_rounding():
    """0.05 short of the stack, inside GATE_AMOUNT_TOLERANCE_BB. The corpus has a
    shove recorded 4.55 against 4.6 behind; see D6."""
    assert check_fva_amount(
        _fva_amount("SB", "all_in", 17.55),
        _stacked(3, {"BB": 20.0, "SB": 17.1, "BTN": 9.0}),
    ) is None


def test_p4_7_a_null_bet_amount_is_not_this_gates_business():
    """There is nothing to compare. The FVA's shape is P4-4's."""
    assert check_fva_amount(
        _fva_amount("SB", "all_in", None),
        _stacked(3, {"BB": 20.0, "SB": 17.1, "BTN": 9.0}),
    ) is None


def test_eligible_seats_at_fva_is_the_seats_from_the_fva_onwards():
    """Preflop order is descending seat number, so 'the FVA and everyone after
    it' is seat_number <= the FVA's."""
    state = {
        "fva": {"seat_number": 2},
        "hand_setup": {"players": [
            {"seat_position_label": "BB", "seat_number": 1},
            {"seat_position_label": "SB", "seat_number": 2},
            {"seat_position_label": "BTN", "seat_number": 3},
        ]},
    }
    assert [p["seat_position_label"] for p in eligible_seats_at_fva(state)] == ["BB", "SB"]


def test_find_pending_hand_setups_raises_when_the_video_has_no_payout_row():
    """A missing tournament_results row is a broken invariant, not a data
    condition: Phase 2's gate records blocked_upstream rather than producing
    clips, so nothing downstream should exist.

    The join is LEFT precisely so this is reachable. An INNER JOIN would drop
    the row and the hand would silently stop being selected — the bounty gates
    would go quiet rather than loud.
    """
    row = MagicMock()
    row.hand_setup_id = "hs1"
    row.video_id = "vid1"
    row.bounty_type = None
    mock_client = _mock_bq_client(rows=[row])

    with pytest.raises(RuntimeError, match="no tournament_results row"):
        _find_pending_hand_setups("proj", "ds", client=mock_client)


def test_find_pending_hand_setups_error_names_the_command_that_fixes_it():
    row = MagicMock()
    row.hand_setup_id = "hs1"
    row.video_id = "vid1"
    row.bounty_type = None
    mock_client = _mock_bq_client(rows=[row])

    with pytest.raises(RuntimeError, match=r"tt extract-payouts --video-id vid1"):
        _find_pending_hand_setups("proj", "ds", client=mock_client)


def test_p4_4_fires_before_any_frame_work():
    """The point of gating here rather than in dbt is the saved call. A bad FVA
    must not pay for frame extraction, the hole-card read, or its retry."""
    bad_fva = {**_CLIP_RESULT_FOUND, "action_type": "fold"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=bad_fva),
        patch("table_talk.hand_start_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_start_processing.call_gemini_for_frame") as mock_frame,
        patch("table_talk.hand_start_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "failed_transient"
    mock_extract.assert_not_called()
    mock_frame.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_starts.assert_not_called()
    assert mock_attempt.call_args[0][0].status_message.startswith(
        "failed_transient: P4-4: invalid_fva_action_type: "
    )


def test_p4_4_parks_at_the_cap_like_any_other_transient():
    bad_fva = {**_CLIP_RESULT_FOUND, "action_type": "check"}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=bad_fva),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
            max_attempts=3,
        ))

    assert outcome == "failed_parked"
    assert "P4-4: invalid_fva_action_type: " in mock_attempt.call_args[0][0].status_message


def test_p4_5_duplicate_in_the_orchestrator_is_transient_and_writes_no_row():
    duplicate = {"players": [
        {"seat_position_label": "BB", "hole_cards": ["Ah", "Kd"]},
        {"seat_position_label": "BTN", "hole_cards": ["Ah", "3c"]},
    ]}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=_CLIP_RESULT_FOUND),
        patch("table_talk.hand_start_processing.extract_frame", side_effect=_fake_extract_frame),
        patch("table_talk.hand_start_processing.call_gemini_for_frame", return_value=duplicate),
        patch("table_talk.hand_start_processing.upload_frame"),
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "failed_transient"
    mock_write_starts.assert_not_called()
    assert "P4-5: duplicate_hole_card: " in mock_attempt.call_args[0][0].status_message


def test_p4_7_fires_before_any_frame_work():
    """Same argument as P4-4's: the saved call is the point. An FVA amount that
    cannot be right must not pay for the ULTRA_HIGH hole-card read — and Phase 5
    can only rediscover this error at a Pro call, where every retry fails
    identically because the fault is upstream of the retry."""
    # BTN holds 50.0 and posts the 0.5 small blind heads-up, so an all-in is 50.5.
    bad_amount = {**_CLIP_RESULT_FOUND, "action_type": "all_in", "bet_amount": 50.0}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=bad_amount),
        patch("table_talk.hand_start_processing.extract_frame") as mock_extract,
        patch("table_talk.hand_start_processing.call_gemini_for_frame") as mock_frame,
        patch("table_talk.hand_start_processing.upload_frame") as mock_upload,
        patch("table_talk.hand_start_processing.write_hand_starts") as mock_write_starts,
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
        ))

    assert outcome == "failed_transient"
    mock_extract.assert_not_called()
    mock_frame.assert_not_called()
    mock_upload.assert_not_called()
    mock_write_starts.assert_not_called()
    assert mock_attempt.call_args[0][0].status_message.startswith(
        "failed_transient: P4-7: fva_amount_mismatch: "
    )


def test_p4_7_parks_at_the_cap_like_any_other_transient():
    bad_amount = {**_CLIP_RESULT_FOUND, "action_type": "all_in", "bet_amount": 50.0}
    with (
        patch("table_talk.hand_start_processing.call_gemini_for_clip", return_value=bad_amount),
        patch("table_talk.hand_start_processing.write_hand_starts"),
        patch("table_talk.hand_start_processing.write_hand_setup_processing_attempt_row") as mock_attempt,
    ):
        outcome = _run(process_hand_setup(
            _HS_AT_CAP, "/tmp/video.mp4", "proj", "ds",
            "videos-bucket", "hand-starts-bucket",
            "identify prompt", "extract prompt",
            prompt_hashes=_P4_HASHES,
            max_attempts=3,
        ))

    assert outcome == "failed_parked"
    assert "P4-7: fva_amount_mismatch: " in mock_attempt.call_args[0][0].status_message
