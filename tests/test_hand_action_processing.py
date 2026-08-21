import asyncio
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from table_talk.gemini_caller import GeminiPermanentError, GeminiTransientError
from table_talk.hand_action_processing import (
    CARD_READ_ATTEMPTS,
    MAX_WINDOW_SECONDS,
    CommunityCardUnreadable,
    PendingHandStart,
    _find_pending_hand_starts,
    _street_cards_unusable,
    _street_timestamp_guard,
    _transient_status,
    check_preconditions,
    process_hand_start,
    process_pending_hand_starts,
)
from table_talk.videos_downloader import DownloadPermanentError

_FVA = {"seat_position_label": "CO", "seat_number": 4, "action_type": "raise", "bet_amount": 2.5}

_ACTIONS_PROMPT = "ACTIONS players={player_context} fva={fva_context}"
_SCAN_PROMPT = "SCAN street={street_name}"
_FRAME_PROMPT = "FRAME prior={prior_cards}"
_REFERENCE_IMAGES = [
    (b"flopimg", "image/jpeg", "flop"),
    (b"turnimg", "image/jpeg", "turn"),
    (b"riverimg", "image/jpeg", "river"),
]


def _hand_start_state(total_seat_count=6, fva=_FVA, players=None):
    if players is None:
        players = [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
             "hole_cards": ["Ah", "Kd"]},
            {"seat_number": 4, "seat_position_label": "CO", "stack_size": 80.0,
             "hole_cards": ["2c", "3c"]},
        ]
    state = {
        "hand_setup": {
            "total_seat_count": total_seat_count,
            "pot_size_bb": 1.5,
            "players": players,
        }
    }
    if fva is not None:
        state["fva"] = dict(fva)
    return state


def _pending(**kwargs) -> PendingHandStart:
    # Window is [100, 160]; the FVA lands at 105.
    defaults = dict(
        hand_start_id="clip_001_001_001",
        hand_setup_id="clip_001_001",
        clip_id="clip_001",
        video_id="vid_a",
        hand_setup_time_seconds=100,
        fva_time_seconds=105,
        hand_start_state=_hand_start_state(),
        raw_lead_gap_seconds=60,
        consecutive_failures=0,
    )
    return PendingHandStart(**{**defaults, **kwargs})


_PREFLOP_ACTIONS = [
    {"action_order": 1, "seat_position_label": "CO", "action_type": "raise", "bet_amount": 2.5},
    {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 2.5},
]


def _d_result(street_names=("preflop",), winning_positions=("CO",), actions=None):
    return {
        "streets": [
            {
                "street_name": name,
                "actions": (actions if actions is not None else _PREFLOP_ACTIONS)
                if name == "preflop"
                else [],
            }
            for name in street_names
        ],
        "winning_positions": list(winning_positions),
    }


def _scan(found=True, timestamp="02:00"):
    return {"found": found, "timestamp": timestamp if found else None}


def _fake_extract_frame(_video_uri, _ts, output_path):
    with open(output_path, "wb") as f:
        f.write(b"\xff\xd8\xff\x00" * 4)


def _run(coro):
    return asyncio.run(coro)


def _mock_bq_client(rows=None):
    mock_job = MagicMock()
    mock_job.result.return_value = rows if rows is not None else []
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client


def _bq_row(**kwargs):
    defaults = dict(
        hand_start_id="clip_001_001_001",
        hand_setup_id="clip_001_001",
        clip_id="clip_001",
        video_id="vid_a",
        hand_start_state=_hand_start_state(),
        fva_time_seconds=105,
        hand_setup_time_seconds=100,
        raw_lead_gap_seconds=60,
        consecutive_failures=0,
    )
    return SimpleNamespace(**{**defaults, **kwargs})


@contextmanager
def _patched(clip_results, frame_results=None):
    with (
        patch(
            "table_talk.hand_action_processing.call_gemini_for_clip", side_effect=clip_results
        ) as clip,
        patch(
            "table_talk.hand_action_processing.call_gemini_for_frame",
            side_effect=frame_results if frame_results is not None else [],
        ) as frame,
        patch(
            "table_talk.hand_action_processing.extract_frame", side_effect=_fake_extract_frame
        ) as extract,
        patch("table_talk.hand_action_processing.upload_frame") as upload,
        patch("table_talk.hand_action_processing.write_hand_actions") as write_actions,
        patch(
            "table_talk.hand_action_processing.write_hand_start_processing_attempt_row"
        ) as write_attempt,
    ):
        yield SimpleNamespace(
            clip=clip, frame=frame, extract=extract, upload=upload,
            write_actions=write_actions, write_attempt=write_attempt,
        )


def _call(hs):
    return _run(
        process_hand_start(
            hs, "/tmp/video.mp4", "proj", "ds", "videos-bucket", "actions-bucket",
            _ACTIONS_PROMPT, _SCAN_PROMPT, _FRAME_PROMPT, _REFERENCE_IMAGES,
        )
    )


def _attempt_row(mocks):
    return mocks.write_attempt.call_args[0][0]


def _written_row(mocks):
    return mocks.write_actions.call_args[0][0][0]


# ---------------------------------------------------------------------------
# check_preconditions
# ---------------------------------------------------------------------------


def test_check_preconditions_passes_valid_hand_start():
    assert check_preconditions(_pending()) is None


def test_check_preconditions_window_at_cap_passes():
    assert check_preconditions(_pending(raw_lead_gap_seconds=MAX_WINDOW_SECONDS)) is None


def test_check_preconditions_window_over_cap_skips():
    reason = check_preconditions(_pending(raw_lead_gap_seconds=MAX_WINDOW_SECONDS + 1))
    assert reason is not None
    assert reason.startswith("skipped:")
    assert "241" in reason


def test_check_preconditions_missing_fva_skips():
    reason = check_preconditions(_pending(hand_start_state=_hand_start_state(fva=None)))
    assert reason is not None
    assert "fva" in reason


def test_check_preconditions_null_fva_seat_position_label_skips():
    state = _hand_start_state(fva={**_FVA, "seat_position_label": None})
    reason = check_preconditions(_pending(hand_start_state=state))
    assert reason is not None
    assert "seat_position_label" in reason


def test_check_preconditions_window_check_wins_over_fva_check():
    # Checks run in order and the first failure wins.
    state = _hand_start_state(fva=None)
    reason = check_preconditions(
        _pending(raw_lead_gap_seconds=MAX_WINDOW_SECONDS + 1, hand_start_state=state)
    )
    assert "raw_lead_gap_seconds" in reason


# ---------------------------------------------------------------------------
# _street_timestamp_guard / _street_cards_unusable / _transient_status
# ---------------------------------------------------------------------------


def test_street_timestamp_guard_in_window_passes():
    _street_timestamp_guard(120, 105, 160, "flop")


def test_street_timestamp_guard_boundaries_pass():
    _street_timestamp_guard(105, 105, 160, "flop")
    _street_timestamp_guard(160, 105, 160, "flop")


@pytest.mark.parametrize("timestamp", [104, 161])
def test_street_timestamp_guard_out_of_window_raises(timestamp):
    with pytest.raises(GeminiPermanentError, match="hallucination"):
        _street_timestamp_guard(timestamp, 105, 160, "turn")


@pytest.mark.parametrize(
    "cards,prior_count,expected_ok",
    [
        (["5d", "8d", "As"], 0, True),
        (["Kh"], 3, True),
        (["2s"], 4, True),
        (["5d", "8d"], 0, False),          # short flop
        (["5d", "8d", "As", "Kh"], 0, False),  # long flop
        ([], 0, False),                     # empty read
        ([None], 3, False),                 # null turn card
        (["5d", None, "As"], 0, False),     # null inside the flop
    ],
)
def test_street_cards_unusable(cards, prior_count, expected_ok):
    assert (_street_cards_unusable(cards, prior_count) is None) is expected_ok


@pytest.mark.parametrize(
    "consecutive_failures,expected",
    [(0, "failed_transient"), (1, "failed_transient"), (2, "failed_parked"), (5, "failed_parked")],
)
def test_transient_status_boundary(consecutive_failures, expected):
    assert _transient_status(consecutive_failures, 3) == expected


# ---------------------------------------------------------------------------
# _find_pending_hand_starts
# ---------------------------------------------------------------------------


def test_find_pending_hand_starts_no_filters():
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "only_video_ids" not in query
    assert "only_hand_start_ids" not in query
    assert client.query.call_args[1]["job_config"].query_parameters == []


def test_find_pending_hand_starts_computes_lead_before_joining_hand_starts():
    # The next hand setup bounds this hand even when it produced no hand_starts
    # row, so the LEAD must be computed over hand_setups in its own CTE.
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    windowed = query.index("WITH windowed AS")
    lead = query.index("LEAD(hs.hand_setup_time_seconds)")
    join = query.index("INNER JOIN windowed w USING (hand_setup_id)")
    assert windowed < lead < join
    # No output-existence guard: several outcomes are terminal with zero rows.
    assert "hand_actions" not in query


def test_find_pending_hand_starts_selects_only_pending_statuses():
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "a.latest_status IS NULL OR a.latest_status = 'failed_transient'" in query


def test_find_pending_hand_starts_video_filter():
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", only_video_ids=["vid_a"], client=client)

    query = client.query.call_args[0][0]
    assert "AND h.video_id IN UNNEST(@only_video_ids)" in query
    params = {p.name: p for p in client.query.call_args[1]["job_config"].query_parameters}
    assert params["only_video_ids"].values == ["vid_a"]


def test_find_pending_hand_starts_hand_start_id_filter():
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", only_hand_start_ids=["a", "b"], client=client)

    query = client.query.call_args[0][0]
    assert "AND h.hand_start_id IN UNNEST(@only_hand_start_ids)" in query
    params = {p.name: p for p in client.query.call_args[1]["job_config"].query_parameters}
    assert params["only_hand_start_ids"].values == ["a", "b"]


def test_find_pending_hand_starts_empty_scope_list_scopes_to_nothing():
    # `is not None`, not truthiness — an explicitly empty list must scope to
    # nothing rather than silently widening to an unscoped scan.
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", only_hand_start_ids=[], client=client)

    assert "AND h.hand_start_id IN UNNEST(@only_hand_start_ids)" in client.query.call_args[0][0]


def test_find_pending_hand_starts_builds_pending_hand_start():
    client = _mock_bq_client([_bq_row()])
    results = _find_pending_hand_starts("proj", "ds", client=client)

    assert len(results) == 1
    hs = results[0]
    assert hs.hand_start_id == "clip_001_001_001"
    assert hs.hand_setup_id == "clip_001_001"
    assert hs.fva_time_seconds == 105
    assert hs.hand_setup_time_seconds == 100
    assert hs.raw_lead_gap_seconds == 60
    assert hs.consecutive_failures == 0


# ---------------------------------------------------------------------------
# process_hand_start — preconditions and step D
# ---------------------------------------------------------------------------


def test_complete_skipped_makes_no_gemini_call_and_writes_no_row():
    with _patched([]) as mocks:
        outcome = _call(_pending(raw_lead_gap_seconds=MAX_WINDOW_SECONDS + 1))

    assert outcome == "complete_skipped"
    mocks.clip.assert_not_called()
    mocks.frame.assert_not_called()
    mocks.write_actions.assert_not_called()
    assert _attempt_row(mocks).status == "complete_skipped"


def test_preflop_only_hand_issues_no_step_e_calls():
    with _patched([_d_result()]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    # One clip call (step D) and zero frame calls — the whole point of running
    # D and E sequentially rather than in parallel.
    assert mocks.clip.call_count == 1
    assert mocks.frame.call_count == 0
    mocks.upload.assert_not_called()

    row = _written_row(mocks)
    assert row.street_frame_gcs_paths == []
    streets = row.hand_action_state["streets"]
    assert [s["street_name"] for s in streets] == ["preflop"]
    assert streets[0]["street_timestamp"] is None
    assert streets[0]["community_cards"] == []
    assert row.hand_action_state["winning_positions"] == ["CO"]


def test_step_d_receives_window_prompts_and_label():
    with _patched([_d_result()]) as mocks:
        _call(_pending())

    args, kwargs = mocks.clip.call_args
    filled_prompt, video_uri, start, end, project = args
    assert video_uri == "gs://videos-bucket/vid_a.mp4"
    assert (start, end) == (100, 160)
    assert project == "proj"
    assert kwargs["label"] == "step_d_player_actions"
    # Both slots substituted; no leftover tokens.
    assert "{player_context}" not in filled_prompt
    assert "{fva_context}" not in filled_prompt
    assert "Hole cards: Ah Kd" in filled_prompt
    assert "Seat 4 (CO)" in filled_prompt


def test_full_river_hand_issues_one_d_call_and_six_e_calls():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),
        _scan(timestamp="02:10"),
        _scan(timestamp="02:20"),
    ]
    frame_results = [
        {"new_cards": ["5d", "8d", "As"]},
        {"new_cards": ["Kh"]},
        {"new_cards": ["2s"]},
    ]
    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.clip.call_count == 4    # D + three scans
    assert mocks.frame.call_count == 3   # three reads
    assert mocks.upload.call_count == 3

    row = _written_row(mocks)
    streets = row.hand_action_state["streets"]
    assert [s["street_name"] for s in streets] == ["preflop", "flop", "turn", "river"]
    assert streets[1]["community_cards"] == ["5d", "8d", "As"]
    assert streets[1]["street_timestamp"] == 120
    assert streets[2]["community_cards"] == ["Kh"]
    assert streets[3]["community_cards"] == ["2s"]
    assert row.street_frame_gcs_paths == [
        "gs://actions-bucket/vid_a/clip_001/clip_001_001/flop.jpg",
        "gs://actions-bucket/vid_a/clip_001/clip_001_001/turn.jpg",
        "gs://actions-bucket/vid_a/clip_001/clip_001_001/river.jpg",
    ]


def test_hand_start_state_nested_verbatim_under_hand_start():
    with _patched([_d_result()]) as mocks:
        _call(_pending())

    state = _written_row(mocks).hand_action_state
    assert state["hand_start"] == _hand_start_state()
    assert set(state) == {"hand_start", "streets", "winning_positions"}


# ---------------------------------------------------------------------------
# A1 — empty winning_positions means the window missed the hand end
# ---------------------------------------------------------------------------


def test_empty_winning_positions_fails_transient_before_any_step_e_call():
    with _patched([_d_result(winning_positions=())]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    # The check fires before step E, so a truncated window costs one call not seven.
    assert mocks.clip.call_count == 1
    mocks.frame.assert_not_called()
    mocks.write_actions.assert_not_called()

    attempt = _attempt_row(mocks)
    assert attempt.status == "failed_transient"
    assert "no winning position observed" in attempt.status_message


def test_missing_winning_positions_key_behaves_as_empty():
    d_result = _d_result()
    del d_result["winning_positions"]

    with _patched([d_result]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert "no winning position observed" in _attempt_row(mocks).status_message


def test_empty_winning_positions_parks_at_cap():
    with _patched([_d_result(winning_positions=())]) as mocks:
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"
    assert _attempt_row(mocks).status == "failed_parked"


# ---------------------------------------------------------------------------
# Step E sequencing and the prior-cards accumulator
# ---------------------------------------------------------------------------


def test_first_scan_starts_at_fva_not_hand_setup_time():
    # The flop cannot precede the first voluntary action; the narrower window is
    # deliberate and cheaper than the PoC's hand_setup_time_seconds start.
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    with _patched(clip_results, [{"new_cards": ["5d", "8d", "As"]}]) as mocks:
        _call(_pending())

    scan_args = mocks.clip.call_args_list[1][0]
    assert scan_args[2] == 105   # fva_time_seconds, not hand_setup_time_seconds (100)
    assert scan_args[3] == 160


def test_each_scan_starts_at_the_previous_street_timestamp():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),
        _scan(timestamp="02:10"),
        _scan(timestamp="02:20"),
    ]
    frame_results = [
        {"new_cards": ["5d", "8d", "As"]},
        {"new_cards": ["Kh"]},
        {"new_cards": ["2s"]},
    ]
    with _patched(clip_results, frame_results) as mocks:
        _call(_pending())

    starts = [call[0][2] for call in mocks.clip.call_args_list[1:]]
    assert starts == [105, 120, 130]


def test_scans_pass_street_name_reference_images_and_label():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    with _patched(clip_results, [{"new_cards": ["5d", "8d", "As"]}]) as mocks:
        _call(_pending())

    scan_call = mocks.clip.call_args_list[1]
    assert scan_call[0][0] == "SCAN street=flop"
    assert scan_call[1]["reference_images"] == _REFERENCE_IMAGES
    assert scan_call[1]["label"] == "step_e_scan_flop"


def test_prior_cards_accumulate_across_streets():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),
        _scan(timestamp="02:10"),
        _scan(timestamp="02:20"),
    ]
    frame_results = [
        {"new_cards": ["5d", "8d", "As"]},
        {"new_cards": ["Kh"]},
        {"new_cards": ["2s"]},
    ]
    with _patched(clip_results, frame_results) as mocks:
        _call(_pending())

    prompts = [call[0][0] for call in mocks.frame.call_args_list]
    assert "(none — 0 prior cards)" in prompts[0]
    assert "(3 prior cards)" in prompts[1]
    assert "- 5d" in prompts[1] and "- As" in prompts[1]
    assert "(4 prior cards)" in prompts[2]
    assert "- Kh" in prompts[2]


def test_frame_extracted_at_street_timestamp_plus_settle_offset():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    with _patched(clip_results, [{"new_cards": ["5d", "8d", "As"]}]) as mocks:
        _call(_pending())

    assert mocks.extract.call_args[0][1] == 120.5


def test_ten_prefixed_community_cards_are_normalized():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    with _patched(clip_results, [{"new_cards": ["10d", "8d", "As"]}]) as mocks:
        _call(_pending())

    streets = _written_row(mocks).hand_action_state["streets"]
    assert streets[1]["community_cards"] == ["Td", "8d", "As"]


# ---------------------------------------------------------------------------
# Null community cards fail the hand — the PoC accumulator bug guard
# ---------------------------------------------------------------------------


def test_null_flop_card_fails_hand_and_never_issues_the_turn_scan():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn")),
        _scan(timestamp="02:00"),
        _scan(timestamp="02:10"),  # must never be consumed
    ]
    frame_results = [{"new_cards": ["5d", None, "As"]}] * CARD_READ_ATTEMPTS

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    # D plus the flop scan only — the turn scan is never reached, so a
    # null-contaminated prior_cards can never reach a downstream read.
    assert mocks.clip.call_count == 2
    assert mocks.frame.call_count == CARD_READ_ATTEMPTS
    mocks.write_actions.assert_not_called()

    attempt = _attempt_row(mocks)
    assert attempt.status == "failed_transient"
    assert "flop" in attempt.status_message
    assert "null card" in attempt.status_message


def test_short_flop_read_fails_hand():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [{"new_cards": ["5d", "8d"]}] * CARD_READ_ATTEMPTS

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert "expected 3 new card(s), got 2" in _attempt_row(mocks).status_message


def test_card_read_retries_then_succeeds():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [
        {"new_cards": ["5d", None, "As"]},
        {"new_cards": ["5d", "8d", "As"]},
    ]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.frame.call_count == 2
    streets = _written_row(mocks).hand_action_state["streets"]
    assert streets[1]["community_cards"] == ["5d", "8d", "As"]


def test_card_read_stops_at_attempt_cap():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [{"new_cards": [None, None, None]}] * (CARD_READ_ATTEMPTS + 2)

    with _patched(clip_results, frame_results) as mocks:
        _call(_pending())

    assert mocks.frame.call_count == CARD_READ_ATTEMPTS


def test_null_card_parks_at_cap():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [{"new_cards": ["5d", None, "As"]}] * CARD_READ_ATTEMPTS

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"
    assert _attempt_row(mocks).status == "failed_parked"


def test_community_card_unreadable_is_transient_not_permanent():
    assert not issubclass(CommunityCardUnreadable, GeminiPermanentError)


# ---------------------------------------------------------------------------
# Scan found: false is how the hand's end is discovered, not a failure
# ---------------------------------------------------------------------------


def test_scan_not_found_truncates_streets_and_still_completes():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),
        _scan(found=False),   # turn, first attempt
        _scan(found=False),   # turn, disagreement retry
    ]
    frame_results = [{"new_cards": ["5d", "8d", "As"]}]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    # D + flop scan + two turn scans. The river is never scanned once the turn
    # is confirmed absent.
    assert mocks.clip.call_count == 4

    row = _written_row(mocks)
    assert [s["street_name"] for s in row.hand_action_state["streets"]] == ["preflop", "flop"]
    assert len(row.street_frame_gcs_paths) == 1

    # The D/E disagreement is the cross-check sequential execution preserves.
    message = _attempt_row(mocks).status_message
    assert "D reported turn" in message
    assert "2 scans found none" in message
    assert "truncated" in message


# ---------------------------------------------------------------------------
# Scan retry on D/E disagreement
#
# A wrong found: true is caught by _street_timestamp_guard; a wrong found: false
# truncates the hand and looks legitimate. Reproduction against a real window
# showed that miss is stochastic — 3 of 4 runs found the street.
# ---------------------------------------------------------------------------


def test_scan_retry_recovers_a_street_the_first_scan_missed():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn")),
        _scan(timestamp="02:00"),          # flop
        _scan(found=False),                # turn, missed
        _scan(timestamp="02:10"),          # turn, found on retry
    ]
    frame_results = [
        {"new_cards": ["5d", "8d", "As"]},
        {"new_cards": ["Kh"]},
    ]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.clip.call_count == 4

    turn_scans = [c for c in mocks.clip.call_args_list if c[0][0] == "SCAN street=turn"]
    assert len(turn_scans) == 2
    # The retry is issued with identical arguments, only the label differs.
    assert turn_scans[0][0] == turn_scans[1][0]
    assert turn_scans[0][1]["label"] == "step_e_scan_turn"
    assert turn_scans[1][1]["label"] == "step_e_scan_turn_retry"

    streets = _written_row(mocks).hand_action_state["streets"]
    assert [s["street_name"] for s in streets] == ["preflop", "flop", "turn"]
    assert streets[2]["community_cards"] == ["Kh"]
    assert streets[2]["street_timestamp"] == 130
    assert "truncated" not in _attempt_row(mocks).status_message


def test_scan_retry_gives_up_after_one_extra_attempt():
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn")),
        _scan(timestamp="02:00"),
        _scan(found=False),
        _scan(found=False),
        _scan(found=False),   # must never be consumed
    ]
    frame_results = [{"new_cards": ["5d", "8d", "As"]}]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.clip.call_count == 4   # exactly one retry, not a loop
    assert [s["street_name"] for s in _written_row(mocks).hand_action_state["streets"]] == [
        "preflop", "flop",
    ]
    assert "2 scans found none" in _attempt_row(mocks).status_message


def test_no_retry_when_d_did_not_report_the_street():
    # D reporting flop only means the turn is never scanned at all, so there is
    # no found: false to retry. Pinned so the retry cannot leak into the normal
    # end-of-hand path, where found: false is the correct answer.
    clip_results = [
        _d_result(street_names=("preflop", "flop")),
        _scan(timestamp="02:00"),
    ]
    frame_results = [{"new_cards": ["5d", "8d", "As"]}]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.clip.call_count == 2   # D + the flop scan only
    scanned = {c[0][0] for c in mocks.clip.call_args_list[1:]}
    assert scanned == {"SCAN street=flop"}
    assert "truncated" not in _attempt_row(mocks).status_message


def test_no_scan_calls_at_all_on_a_preflop_ending_hand():
    with _patched([_d_result(street_names=("preflop",))]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.clip.call_count == 1
    assert mocks.frame.call_count == 0


def test_recovered_street_advances_scan_start_and_prior_cards_normally():
    # A retried success must advance the chain exactly as a first-attempt success
    # does: the next scan starts at the recovered street's timestamp, and its
    # cards land in prior_cards.
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),          # flop -> 120
        _scan(found=False),                # turn missed
        _scan(timestamp="02:10"),          # turn recovered -> 130
        _scan(timestamp="02:20"),          # river -> 140
    ]
    frame_results = [
        {"new_cards": ["5d", "8d", "As"]},
        {"new_cards": ["Kh"]},
        {"new_cards": ["2s"]},
    ]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"

    # The river scan starts at the recovered turn timestamp, not the flop's.
    river_scan = mocks.clip.call_args_list[4]
    assert river_scan[0][0] == "SCAN street=river"
    assert river_scan[0][2] == 130

    # prior_cards reached the river read with all four earlier cards.
    river_prompt = mocks.frame.call_args_list[2][0][0]
    assert "(4 prior cards)" in river_prompt
    assert "- Kh" in river_prompt

    streets = _written_row(mocks).hand_action_state["streets"]
    assert [s["street_name"] for s in streets] == ["preflop", "flop", "turn", "river"]
    assert streets[3]["community_cards"] == ["2s"]


def test_status_message_records_window_and_streets_on_clean_run():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    with _patched(clip_results, [{"new_cards": ["5d", "8d", "As"]}]) as mocks:
        _call(_pending())

    message = _attempt_row(mocks).status_message
    assert message.startswith("complete: window=60s streets=preflop,flop")
    assert "truncated" not in message


# ---------------------------------------------------------------------------
# Heads-up label rewriting
# ---------------------------------------------------------------------------


def test_heads_up_rewrites_sb_in_actions_and_winning_positions():
    state = _hand_start_state(
        total_seat_count=2,
        players=[
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
             "hole_cards": ["Ah", "Kd"]},
            {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 80.0,
             "hole_cards": ["2c", "3c"]},
        ],
    )
    d_result = _d_result(
        winning_positions=("SB",),
        actions=[
            {"action_order": 1, "seat_position_label": "SB",
             "action_type": "raise", "bet_amount": 2.5},
            {"action_order": 2, "seat_position_label": "BB",
             "action_type": "call", "bet_amount": 2.5},
        ],
    )

    with _patched([d_result]) as mocks:
        outcome = _call(_pending(hand_start_state=state))

    assert outcome == "complete"
    hand_action_state = _written_row(mocks).hand_action_state
    assert hand_action_state["winning_positions"] == ["BTN"]
    labels = [a["seat_position_label"] for a in hand_action_state["streets"][0]["actions"]]
    assert labels == ["BTN", "BB"]


def test_six_handed_sb_is_not_rewritten():
    d_result = _d_result(
        winning_positions=("SB",),
        actions=[
            {"action_order": 1, "seat_position_label": "SB",
             "action_type": "raise", "bet_amount": 2.5},
        ],
    )

    with _patched([d_result]) as mocks:
        _call(_pending())

    hand_action_state = _written_row(mocks).hand_action_state
    assert hand_action_state["winning_positions"] == ["SB"]
    assert hand_action_state["streets"][0]["actions"][0]["seat_position_label"] == "SB"


# ---------------------------------------------------------------------------
# Error classification and the no-clobber rule
# ---------------------------------------------------------------------------


def test_gemini_permanent_error_is_failed_permanent():
    with _patched(GeminiPermanentError("malformed JSON from Gemini")) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    assert _attempt_row(mocks).status == "failed_permanent"
    mocks.write_actions.assert_not_called()


def test_gemini_transient_error_is_failed_transient():
    with _patched(GeminiTransientError("rate limited")) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    mocks.write_actions.assert_not_called()


def test_hallucinated_street_timestamp_is_failed_permanent():
    clip_results = [
        _d_result(street_names=("preflop", "flop")),
        _scan(timestamp="09:99"),  # 599s, far outside [105, 160]
    ]
    with _patched(clip_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    assert "hallucination" in _attempt_row(mocks).status_message


def test_gemini_permanent_error_unaffected_by_retry_cap():
    with _patched(GeminiPermanentError("boom")):
        outcome = _call(_pending(consecutive_failures=5))

    assert outcome == "failed_permanent"


def test_catch_all_exception_parks_at_cap():
    with _patched(RuntimeError("something else broke")):
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"


@pytest.mark.parametrize(
    "clip_results,frame_results",
    [
        (GeminiPermanentError("boom"), None),
        (RuntimeError("boom"), None),
        ([_d_result(winning_positions=())], None),
        (
            [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")],
            [{"new_cards": ["5d", None, "As"]}] * CARD_READ_ATTEMPTS,
        ),
    ],
)
def test_failures_never_clear_an_existing_row(clip_results, frame_results):
    # ARCHITECTURE.md: failures never delete existing output, so a stage row
    # legitimately coexists with a later failed_transient or failed_parked
    # attempt. A failure must not call the writer at all — not even with [].
    with _patched(clip_results, frame_results) as mocks:
        _call(_pending())

    mocks.write_actions.assert_not_called()


def test_complete_always_writes_exactly_one_row():
    # Phase 5 has no successful zero-row outcome: a hand that ends preflop still
    # has a preflop street, so `complete` always means one row.
    with _patched([_d_result()]) as mocks:
        _call(_pending())

    mocks.write_actions.assert_called_once()
    rows, = mocks.write_actions.call_args[0]
    assert len(rows) == 1
    assert mocks.write_actions.call_args[1]["hand_start_id"] == "clip_001_001_001"


# ---------------------------------------------------------------------------
# process_pending_hand_starts
# ---------------------------------------------------------------------------


def _run_pending(**kwargs):
    return _run(
        process_pending_hand_starts(
            "proj", "ds", "videos-bucket", "actions-bucket",
            _ACTIONS_PROMPT, _SCAN_PROMPT, _FRAME_PROMPT, _REFERENCE_IMAGES,
            **kwargs,
        )
    )


def test_process_pending_hand_starts_dispatch():
    hand_starts = [
        _pending(hand_start_id="a", video_id="vid_a"),
        _pending(hand_start_id="b", video_id="vid_a"),
        _pending(hand_start_id="c", video_id="vid_b"),
    ]
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts",
            return_value=hand_starts,
        ),
        patch("table_talk.hand_action_processing.download_video") as mock_download,
        patch(
            "table_talk.hand_action_processing.process_hand_start",
            new_callable=AsyncMock, return_value="complete",
        ) as mock_process,
    ):
        stats = _run_pending()

    assert mock_download.call_count == 2
    assert mock_process.call_count == 3
    assert stats["hand_starts_processed"] == 3
    assert stats["hand_starts_complete"] == 3
    assert stats["hand_starts_failed_transient"] == 0


def test_process_pending_hand_starts_scope_params_translated():
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts", return_value=[]
        ) as mock_find,
    ):
        _run_pending(video_id="vid_a", only_hand_start_ids=["x"])

    kwargs = mock_find.call_args[1]
    assert kwargs["only_video_ids"] == ["vid_a"]
    assert kwargs["only_hand_start_ids"] == ["x"]


def test_process_pending_hand_starts_no_video_id_means_no_video_scope():
    with patch(
        "table_talk.hand_action_processing._find_pending_hand_starts", return_value=[]
    ) as mock_find:
        _run_pending()

    assert mock_find.call_args[1]["only_video_ids"] is None


def test_process_pending_hand_starts_download_failure_marks_transient():
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts",
            return_value=[_pending()],
        ),
        patch(
            "table_talk.hand_action_processing.download_video",
            side_effect=RuntimeError("network"),
        ),
        patch("table_talk.hand_action_processing._write_attempt") as mock_attempt,
    ):
        stats = _run_pending()

    assert stats["hand_starts_failed_transient"] == 1
    assert mock_attempt.call_args[0][1] == "failed_transient"
    assert "video_download_failed" in mock_attempt.call_args[0][2]


def test_process_pending_hand_starts_download_failure_parks_at_cap():
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts",
            return_value=[_pending(consecutive_failures=2)],
        ),
        patch(
            "table_talk.hand_action_processing.download_video",
            side_effect=RuntimeError("network"),
        ),
        patch("table_talk.hand_action_processing._write_attempt") as mock_attempt,
    ):
        stats = _run_pending()

    assert stats["hand_starts_failed_parked"] == 1
    assert mock_attempt.call_args[0][1] == "failed_parked"


def test_process_pending_hand_starts_download_not_found_marks_permanent():
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts",
            return_value=[_pending()],
        ),
        patch(
            "table_talk.hand_action_processing.download_video",
            side_effect=DownloadPermanentError("404"),
        ),
        patch("table_talk.hand_action_processing._write_attempt") as mock_attempt,
    ):
        stats = _run_pending()

    assert stats["hand_starts_failed_permanent"] == 1
    assert mock_attempt.call_args[0][1] == "failed_permanent"
    assert "video_download_not_found" in mock_attempt.call_args[0][2]


def test_process_pending_hand_starts_respects_max_attempts_override():
    with (
        patch(
            "table_talk.hand_action_processing._find_pending_hand_starts",
            return_value=[_pending(consecutive_failures=0)],
        ),
        patch(
            "table_talk.hand_action_processing.download_video",
            side_effect=RuntimeError("network"),
        ),
        patch("table_talk.hand_action_processing._write_attempt") as mock_attempt,
    ):
        stats = _run_pending(max_attempts=1)

    assert stats["hand_starts_failed_parked"] == 1
    assert mock_attempt.call_args[0][1] == "failed_parked"


# ---------------------------------------------------------------------------
# Integration tests — require terraform apply and GCP dev credentials.
#
# Heavy imports are deferred inside each test so the unit suite does not pay for
# them. Setup goes through each earlier phase's production writer, never its
# orchestrator, per CLAUDE.md's cross-phase setup rule.
# ---------------------------------------------------------------------------

_INTEGRATION_PROJECT = "table-talk-497020"
_INTEGRATION_DATASET = "table_talk_dev"


def _seed_hand_start(bq_client, *, hand_setup_time_seconds=0, duration_seconds=60, uid_tag="p5"):
    """Create videos -> clip_manifest -> hand_setups -> hand_starts for one hand.

    duration_seconds drives raw_lead_gap_seconds: with a single hand_setups row
    the pending query's LEAD finds no next hand and falls back to the video's
    duration, so the window is (duration_seconds - hand_setup_time_seconds).
    """
    from table_talk._generated.hand_setups_row import HandSetupsRow
    from table_talk._generated.hand_starts_row import HandStartsRow
    from table_talk.clip_manifest_writer import ClipManifestRow, write_clip_manifest_rows
    from table_talk.hand_setups_writer import write_hand_setups
    from table_talk.hand_starts_writer import write_hand_starts
    from table_talk.videos_writer import VideosRow, write_video_row

    uid = uuid.uuid4().hex[:10]
    video_id = f"test_{uid_tag}_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    hand_start_id = f"{hand_setup_id}_001"

    hand_setup_state = {
        "total_seat_count": 6,
        "pot_size_bb": 1.5,
        "players": [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
             "hole_cards": ["Ah", "Kd"]},
            {"seat_number": 4, "seat_position_label": "CO", "stack_size": 80.0,
             "hole_cards": ["2c", "3c"]},
        ],
    }

    # project= for the Phase 1/2 writers, project_id= for the stage writers —
    # the known keyword asymmetry, honoured rather than normalized.
    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Phase 5 integration test",
            duration_seconds=duration_seconds,
            gcs_path=f"gs://fake-bucket/{video_id}.mp4",
            file_size_bytes=1,
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )
    write_clip_manifest_rows(
        [
            ClipManifestRow(
                clip_id=clip_id,
                video_id=video_id,
                clip_start_time=0,
                clip_end_time=duration_seconds,
            )
        ],
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )
    write_hand_setups(
        [
            HandSetupsRow(
                hand_setup_id=hand_setup_id,
                clip_id=clip_id,
                video_id=video_id,
                hand_setup_time_seconds=hand_setup_time_seconds,
                frame_gcs_path=f"gs://fake-bucket/{hand_setup_id}.jpg",
                hand_setup_state=hand_setup_state,
            )
        ],
        clip_id=clip_id,
        project_id=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )
    write_hand_starts(
        [
            HandStartsRow(
                hand_start_id=hand_start_id,
                hand_setup_id=hand_setup_id,
                clip_id=clip_id,
                video_id=video_id,
                fva_time_seconds=hand_setup_time_seconds + 2,
                second_action_time_seconds=hand_setup_time_seconds + 4,
                hand_start_state={
                    "hand_setup": hand_setup_state,
                    "fva": {
                        "seat_position_label": "CO",
                        "seat_number": 4,
                        "action_type": "raise",
                        "bet_amount": 2.5,
                    },
                },
                fva_frame_gcs_path=f"gs://fake-bucket/{hand_setup_id}_fva.jpg",
                verify_frame_gcs_paths=[f"gs://fake-bucket/{hand_setup_id}_verify_000.jpg"],
            )
        ],
        hand_setup_id=hand_setup_id,
        project_id=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )
    return SimpleNamespace(
        video_id=video_id,
        clip_id=clip_id,
        hand_setup_id=hand_setup_id,
        hand_start_id=hand_start_id,
    )


def _upload_fixture_video(gcs_client, videos_bucket, video_id, duration_seconds=1):
    """Generate a lavfi test video, upload it, and return the blob for cleanup.

    process_pending_hand_starts downloads a video once per video *before*
    dispatching to process_hand_start, so every integration test that reaches the
    orchestrator needs an object in GCS — including one whose hand skips on
    preconditions, because the download precedes the precondition check.

    The file's own duration is irrelevant to the pending query, which reads
    duration_seconds off the videos row, so callers that never decode the video
    can leave this at one second.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_path = os.path.join(tmpdir, "fixture.mp4")
        subprocess.run(
            ["ffmpeg", "-f", "lavfi",
             "-i", f"testsrc=duration={duration_seconds}:size=320x240",
             "-y", fixture_path],
            check=True,
            capture_output=True,
        )
        with open(fixture_path, "rb") as fh:
            video_bytes = fh.read()

    blob = gcs_client.bucket(videos_bucket).blob(f"{video_id}.mp4")
    blob.upload_from_string(video_bytes, content_type="video/mp4")
    return blob


def _write_hand_start_attempt(bq_client, hand_start_id, status):
    from table_talk._generated.hand_start_processing_attempts_row import (
        HandStartProcessingAttemptsRow,
    )
    from table_talk.hand_start_processing_attempts_writer import (
        write_hand_start_processing_attempt_row,
    )

    write_hand_start_processing_attempt_row(
        HandStartProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_start_id=hand_start_id,
            status=status,
            status_message=status,
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )


def _query_rows(bq_client, sql, **params):
    from google.cloud import bigquery as bq

    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter(name, "STRING", value) for name, value in params.items()
        ]
    )
    return list(bq_client.query(sql, job_config=job_config).result())


def _cleanup_hand_start(bq_client, ids):
    """Delete in reverse dependency order: deepest stage table first, videos last."""
    from google.cloud import bigquery as bq

    prefix = f"{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}"
    for table, column, value in [
        (f"{prefix}.hand_actions", "hand_start_id", ids.hand_start_id),
        (f"{prefix}.hand_start_processing_attempts", "hand_start_id", ids.hand_start_id),
        (f"{prefix}.hand_starts", "hand_setup_id", ids.hand_setup_id),
        (f"{prefix}.hand_setup_processing_attempts", "hand_setup_id", ids.hand_setup_id),
        (f"{prefix}.hand_setups", "hand_setup_id", ids.hand_setup_id),
        (f"{prefix}.clip_manifest", "clip_id", ids.clip_id),
        (f"{prefix}.videos", "video_id", ids.video_id),
    ]:
        bq_client.query(
            f"DELETE FROM `{table}` WHERE {column} = @val",
            job_config=bq.QueryJobConfig(
                query_parameters=[bq.ScalarQueryParameter("val", "STRING", value)]
            ),
        ).result()


@pytest.mark.integration
def test_process_pending_hand_starts_precondition_skip_integration():
    """The deterministic counterpart to the happy-path test below.

    The happy-path test accepts several outcomes because Gemini is stochastic
    against lavfi content, so it can pass while the pending query, the writers or
    the cleanup are subtly wrong as long as something got written. This one has
    exactly one correct answer and costs no Gemini call.

    It also covers a path nothing else reaches: the unit tests build
    PendingHandStart by hand, so only this proves the pending query populates
    raw_lead_gap_seconds from the LEAD-and-duration_seconds fallback and hands it
    to the real check_preconditions.

    A fixture video is still required. process_pending_hand_starts downloads once
    per video before dispatching to process_hand_start, so without an object in
    GCS the per-video DownloadPermanentError branch writes failed_permanent for
    every hand and the precondition is never reached. The download is amortised
    across a video's hands in production, so it is not worth reordering the
    orchestrator to skip it — see CLAUDE.md section 2.
    """
    from google.cloud import bigquery as bq
    from google.cloud import storage as gcs

    videos_bucket = "table-talk-497020-videos-dev"

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    gcs_client = gcs.Client()

    # One hand_setups row, so LEAD falls back to duration_seconds: 500 - 100 = 400,
    # comfortably past MAX_WINDOW_SECONDS.
    ids = _seed_hand_start(
        bq_client, hand_setup_time_seconds=100, duration_seconds=500, uid_tag="p5skip"
    )
    expected_gap = 400
    assert expected_gap > MAX_WINDOW_SECONDS

    # Uploaded before the try, so finally covers its deletion. One second is
    # enough — the hand skips before anything decodes it.
    video_blob = _upload_fixture_video(gcs_client, videos_bucket, ids.video_id)

    try:
        stats = _run(
            process_pending_hand_starts(
                project_id=_INTEGRATION_PROJECT,
                dataset=_INTEGRATION_DATASET,
                videos_bucket=videos_bucket,
                hand_actions_bucket="table-talk-497020-hand-actions-dev",
                extract_player_actions_prompt="UNUSED {player_context} {fva_context}",
                extract_community_cards_prompt="UNUSED {street_name}",
                extract_community_cards_from_frame_prompt="UNUSED {prior_cards}",
                reference_images=[],
                only_hand_start_ids=[ids.hand_start_id],
                bq_client=bq_client,
                gcs_client=gcs_client,
            )
        )

        assert stats["hand_starts_processed"] == 1
        assert stats["hand_starts_complete_skipped"] == 1
        assert stats["hand_starts_complete"] == 0

        attempts = _query_rows(
            bq_client,
            f"SELECT status, status_message FROM "
            f"`{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.hand_start_processing_attempts` "
            f"WHERE hand_start_id = @hand_start_id",
            hand_start_id=ids.hand_start_id,
        )
        assert len(attempts) == 1
        assert attempts[0].status == "complete_skipped"
        assert str(expected_gap) in attempts[0].status_message
        assert "MAX_WINDOW_SECONDS" in attempts[0].status_message

        action_rows = _query_rows(
            bq_client,
            f"SELECT hand_start_id FROM "
            f"`{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.hand_actions` "
            f"WHERE hand_start_id = @hand_start_id",
            hand_start_id=ids.hand_start_id,
        )
        assert action_rows == []
    finally:
        _cleanup_hand_start(bq_client, ids)
        if video_blob.exists():
            video_blob.delete()


@pytest.mark.integration
def test_process_pending_hand_starts_integration():
    """Full path against real infrastructure: pending query, Gemini, ffmpeg, GCS.

    The outcome is deliberately loose — lavfi test-pattern content has no poker in
    it, so Gemini's answer is stochastic. What this proves is that the whole chain
    is wired correctly end to end, including that the reference_images tuples and
    the video request reach the API in a shape it accepts.
    """
    from google.cloud import bigquery as bq
    from google.cloud import storage as gcs

    from table_talk.reference_images import load_reference_images

    videos_bucket = "table-talk-497020-videos-dev"
    hand_actions_bucket = "table-talk-497020-hand-actions-dev"

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    gcs_client = gcs.Client()

    ids = _seed_hand_start(
        bq_client, hand_setup_time_seconds=0, duration_seconds=60, uid_tag="p5"
    )

    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    references_dir = Path(__file__).resolve().parents[1] / "references"

    # Uploaded before the try, so finally covers its deletion.
    video_blob = _upload_fixture_video(
        gcs_client, videos_bucket, ids.video_id, duration_seconds=60
    )

    try:
        stats = _run(
            process_pending_hand_starts(
                project_id=_INTEGRATION_PROJECT,
                dataset=_INTEGRATION_DATASET,
                videos_bucket=videos_bucket,
                hand_actions_bucket=hand_actions_bucket,
                extract_player_actions_prompt=(
                    prompts_dir / "extract_player_actions.md"
                ).read_text(),
                extract_community_cards_prompt=(
                    prompts_dir / "extract_community_cards.md"
                ).read_text(),
                extract_community_cards_from_frame_prompt=(
                    prompts_dir / "extract_community_cards_from_frame.md"
                ).read_text(),
                reference_images=load_reference_images(references_dir),
                only_hand_start_ids=[ids.hand_start_id],
                bq_client=bq_client,
                gcs_client=gcs_client,
            )
        )

        assert stats["hand_starts_processed"] == 1

        attempts = _query_rows(
            bq_client,
            f"SELECT status, status_message FROM "
            f"`{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.hand_start_processing_attempts` "
            f"WHERE hand_start_id = @hand_start_id ORDER BY attempted_at DESC LIMIT 1",
            hand_start_id=ids.hand_start_id,
        )
        assert len(attempts) == 1
        latest = attempts[0]
        assert latest.status in (
            "complete",
            "complete_skipped",
            "failed_transient",
            "failed_permanent",
        ), f"unexpected status {latest.status!r}: {latest.status_message}"

        action_rows = _query_rows(
            bq_client,
            f"SELECT hand_start_id, street_frame_gcs_paths FROM "
            f"`{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.hand_actions` "
            f"WHERE hand_start_id = @hand_start_id",
            hand_start_id=ids.hand_start_id,
        )
        # Phase 5 has no successful zero-row outcome, and failures never write.
        if latest.status == "complete":
            assert len(action_rows) == 1
        else:
            assert action_rows == []
    finally:
        _cleanup_hand_start(bq_client, ids)
        if video_blob.exists():
            video_blob.delete()
        for blob in gcs_client.bucket(hand_actions_bucket).list_blobs(
            prefix=f"{ids.video_id}/"
        ):
            blob.delete()


# ---------------------------------------------------------------------------
# _find_pending_hand_starts — attempt-state selection against real BigQuery.
#
# These exercise the CTE's latest-status filter and the consecutive-failures
# counter, neither of which a mocked client can prove.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_find_pending_hand_starts_no_attempts_selected_zero_count():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    try:
        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert len(results) == 1
        assert results[0].consecutive_failures == 0
        # Hydration from the LEAD-and-duration_seconds fallback.
        assert results[0].raw_lead_gap_seconds == 60
        assert results[0].hand_setup_time_seconds == 0
        assert results[0].fva_time_seconds == 2
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_three_transient_selected_count_three():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    try:
        for _ in range(3):
            _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert len(results) == 1
        assert results[0].consecutive_failures == 3
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_transient_then_complete_then_transient_counts_one():
    """The reset case: the counter is consecutive failures since the last
    non-failure, not the lifetime total, or a healthy hand parks early."""
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    try:
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "complete")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert len(results) == 1
        assert results[0].consecutive_failures == 1
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_complete_then_transient_then_complete_not_selected():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    try:
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "complete")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "complete")

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert results == []
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_latest_parked_not_selected():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    try:
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient")
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_parked")

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert results == []
    finally:
        _cleanup_hand_start(bq_client, ids)
