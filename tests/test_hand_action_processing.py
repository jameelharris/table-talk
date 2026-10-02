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

from table_talk.gemini_caller import (
    CLIP_MEDIA_RESOLUTION,
    FRAME_RESOLUTION_ULTRA_HIGH,
    GeminiPermanentError,
    GeminiTransientError,
)
from table_talk.hand_action_processing import (
    CARD_READ_ATTEMPTS,
    MAX_WINDOW_SECONDS,
    CommunityCardUnreadable,
    PendingHandStart,
    StreetTimestampUnusable,
    _board_duplicate,
    _find_pending_hand_starts,
    _gate_failure_outcome,
    _repeats_previous_gate_failure,
    _street_cards_unusable,
    _street_timestamp_guard,
    _transient_status,
    check_missing_hole_cards,
    check_preconditions,
    check_step_d_output,
    process_hand_start,
    process_pending_hand_starts,
)
from table_talk.mark_pending import MARK_MESSAGE_PREFIX, MARK_STATUS
from table_talk.provenance import hash_files
from table_talk.reference_images import STREET_REFERENCE_ORDER, reference_image_filename
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


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_prompt_hashes() -> dict[str, str]:
    """What the CLI would pass, computed from the files on disk.

    Real rather than the stub `_P5_HASHES` the unit tests use, so the integration
    tests exercise provenance end to end: step E's prompts and all three
    reference images are listed, because that is the set a `complete` row names,
    and `select` raises on a path the caller never hashed.
    """
    prompts_dir = _REPO_ROOT / "prompts"
    references_dir = _REPO_ROOT / "references"
    return hash_files(
        [
            prompts_dir / "extract_player_actions.md",
            prompts_dir / "identify_community_cards.md",
            prompts_dir / "extract_community_cards.md",
            *(references_dir / reference_image_filename(street)
              for street in STREET_REFERENCE_ORDER),
        ],
        _REPO_ROOT,
    )


_P5_HASHES = {
    "prompts/extract_player_actions.md": "111111111111",
    "prompts/identify_community_cards.md": "222222222222",
    "prompts/extract_community_cards.md": "333333333333",
    "references/flop_reference.jpeg": "444444444444",
    "references/turn_reference.jpeg": "555555555555",
    "references/river_reference.jpeg": "666666666666",
}

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
        previous_status_message=None,
    )
    return PendingHandStart(**{**defaults, **kwargs})


_PREFLOP_ACTIONS = [
    {"action_order": 1, "seat_position_label": "CO", "action_type": "raise", "bet_amount": 2.5},
    {"action_order": 2, "seat_position_label": "BB", "action_type": "call", "bet_amount": 2.5},
]


# A legal, complete postflop street: both live seats act and end matched.
#
# The old fixture put [] on every postflop street, which is a physically
# impossible hand — two seats live with chips, neither acting, and the hand
# carrying on regardless. P5-7c catches exactly that, so the fixtures had to
# become hands that could actually happen.
_POSTFLOP_ACTIONS = [
    {"action_order": 1, "seat_position_label": "BB", "action_type": "check", "bet_amount": 0.0},
    {"action_order": 2, "seat_position_label": "CO", "action_type": "bet", "bet_amount": 3.0},
    {"action_order": 3, "seat_position_label": "BB", "action_type": "call", "bet_amount": 3.0},
]


def _d_result(
    street_names=("preflop",),
    winning_positions=("CO",),
    actions=None,
    postflop_actions=None,
):
    """Step D output. Postflop streets get a complete check-bet-call by default;
    pass postflop_actions=[] to build a runout (which must be preceded by a
    called all-in preflop, or P5-7c will correctly reject it)."""
    default_postflop = _POSTFLOP_ACTIONS if postflop_actions is None else postflop_actions
    return {
        "streets": [
            {
                "street_name": name,
                "actions": (actions if actions is not None else _PREFLOP_ACTIONS)
                if name == "preflop"
                else [dict(a) for a in default_postflop],
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
        previous_status_message=None,
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
            prompt_hashes=_P5_HASHES,
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
    _street_timestamp_guard(120, 105, 160, "flop", strict_after=False)


def test_street_timestamp_guard_boundaries_pass():
    _street_timestamp_guard(105, 105, 160, "flop", strict_after=False)
    _street_timestamp_guard(160, 105, 160, "flop", strict_after=False)


@pytest.mark.parametrize("timestamp", [104, 161])
def test_street_timestamp_guard_out_of_window_raises(timestamp):
    with pytest.raises(StreetTimestampUnusable, match="hallucination"):
        _street_timestamp_guard(timestamp, 105, 160, "turn", strict_after=True)


def test_p5_13_a_later_street_must_be_strictly_after_the_one_before():
    """Two reveals in the same second cannot happen: every scanned street now
    has betting between it and the one before, because the runouts that had
    none are skipped as inert."""
    with pytest.raises(StreetTimestampUnusable, match="P5-13: street_timestamp_order"):
        _street_timestamp_guard(120, 120, 160, "turn", strict_after=True)


def test_p5_13_the_flop_may_share_a_second_with_the_fva():
    """The flop's scan_start is the FVA, not a previous reveal, so equality
    there is legitimate and must not be rejected."""
    _street_timestamp_guard(105, 105, 160, "flop", strict_after=False)


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
    # Only the mark-message prefix, which is bound on every call: it is what
    # tells the previous *real* attempt from a deliberate reprocess.
    params = {p.name for p in client.query.call_args[1]["job_config"].query_parameters}
    assert params == {"mark_message_prefix"}


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
    assert (
        "a.latest_status IS NULL\n"
        "               OR a.latest_status IN ('failed_transient', 'marked_pending')"
    ) in query


def test_find_pending_hand_starts_treats_a_mark_as_a_non_failure():
    """The mark prefix now has a second use in this query: it keeps a mark from
    standing in for the previous real attempt, and it keeps a mark from
    advancing the retry cap."""
    client = _mock_bq_client()
    _find_pending_hand_starts("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert query.count("status_message LIKE CONCAT(@mark_message_prefix, '%')") == 2


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
    assert hs.previous_status_message is None


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
    # The FVA line specifically, not just "Seat 4 (CO)" — that substring also
    # matches the player-context line and so would pass on an empty fva slot.
    assert "Seat 4 (CO)\nAction: raise 2.5 BB\n" in filled_prompt
    # The anchor the prompt tells the model to begin recording at.
    assert "Occurs at: 01:45 (105s, absolute broadcast time)" in filled_prompt


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
    # Verbatim: provenance is a sibling of this phase's contribution, so the
    # nested upstream blob is untouched. If provenance were merged into
    # hand_start instead, this equality would fail — which is the point.
    assert state["hand_start"] == _hand_start_state()
    assert set(state) == {"hand_start", "streets", "winning_positions", "provenance"}


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


def test_street_cards_are_read_at_ultra_high_resolution():
    """Same resolution as Phase 4's step C, and for the same reason.

    No board misread reproduced in the baseline, so this is prophylactic rather
    than a measured repair here — but it is the same frame read of the same
    four-colour deck, and a board error invalidates the hand for every player
    rather than one seat.
    """
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]

    with _patched(clip_results, [{"new_cards": ["5d", "8d", "As"]}]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    read_call = mocks.frame.call_args_list[0]
    assert read_call.kwargs["frame_media_resolution"] == FRAME_RESOLUTION_ULTRA_HIGH


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


def test_an_unread_contested_street_fails_the_attempt():
    """P5-14 reverses this hand's old outcome.

    It used to write a `complete` row with the streets truncated at the turn
    and the disagreement recorded only in status_message — a hand that looks
    finished and ends a street early, which corrupts an aggregate rather than
    thinning it. Betting was still live on the turn here, so the street is real
    and the right answer is a retry.
    """
    clip_results = [
        _d_result(street_names=("preflop", "flop", "turn", "river")),
        _scan(timestamp="02:00"),
        _scan(found=False),   # turn, first attempt
        _scan(found=False),   # turn, disagreement retry
    ]
    frame_results = [{"new_cards": ["5d", "8d", "As"]}]

    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    # D + flop scan + two turn scans. The river is never scanned once the turn
    # is confirmed absent.
    assert mocks.clip.call_count == 4
    # Failures never write a stage row, and never clear an existing one.
    mocks.write_actions.assert_not_called()

    message = _attempt_row(mocks).status_message
    assert "P5-14: contested_street_unread: " in message
    assert "D reported turn" in message
    assert "2 scans found none" in message


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

    # The retry cap is what this test is about; P5-14 decides the outcome.
    assert outcome == "failed_transient"
    assert mocks.clip.call_count == 4   # exactly one retry, not a loop
    mocks.write_actions.assert_not_called()
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
        # Phase 4 runs normalize_heads_up before writing, so a stored heads-up
        # FVA is already BTN. The point of this test is that Phase 5 rewrites
        # step D's "SB" to agree with it.
        fva={"seat_position_label": "BTN", "seat_number": 3,
             "action_type": "raise", "bet_amount": 2.5},
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
    """The rewrite is heads-up only. At six-handed the SB is a real seat and
    must survive untouched."""
    state = _hand_start_state(
        total_seat_count=6,
        fva={"seat_position_label": "SB", "seat_number": 2,
             "action_type": "raise", "bet_amount": 2.5},
        players=[
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
             "hole_cards": ["Ah", "Kd"]},
            {"seat_number": 2, "seat_position_label": "SB", "stack_size": 90.0,
             "hole_cards": ["Qs", "Qd"]},
        ],
    )
    d_result = _d_result(
        winning_positions=("SB",),
        actions=[
            {"action_order": 1, "seat_position_label": "SB",
             "action_type": "raise", "bet_amount": 2.5},
            {"action_order": 2, "seat_position_label": "BB",
             "action_type": "fold", "bet_amount": 0.0},
        ],
    )

    with _patched([d_result]) as mocks:
        _call(_pending(hand_start_state=state))

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


def test_hallucinated_street_timestamp_is_failed_transient():
    """Reclassified from permanent along with P5-13's strictness.

    The only hallucinated timestamp ever observed — one flop in the original
    corpus run — was caught by this guard and then resolved on a rerun, so
    permanent was the wrong class for the single instance there is evidence
    about, and it denied the retry that actually fixed it.
    """
    clip_results = [
        _d_result(street_names=("preflop", "flop")),
        _scan(timestamp="09:99"),  # 599s, far outside [105, 160]
    ]
    with _patched(clip_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    message = _attempt_row(mocks).status_message
    assert "P5-13: street_timestamp_order: " in message
    assert "hallucination" in message


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
            prompt_hashes=_P5_HASHES,
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
        video_id=video_id,
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


def _write_hand_start_attempt(bq_client, hand_start_id, status, status_message=None):
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
            status_message=status if status_message is None else status_message,
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
                identify_community_cards_prompt="UNUSED {street_name}",
                extract_community_cards_prompt="UNUSED {prior_cards}",
                reference_images=[],
                prompt_hashes=_real_prompt_hashes(),
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
                identify_community_cards_prompt=(
                    prompts_dir / "identify_community_cards.md"
                ).read_text(),
                extract_community_cards_prompt=(
                    prompts_dir / "extract_community_cards.md"
                ).read_text(),
                reference_images=load_reference_images(references_dir),
                prompt_hashes=_real_prompt_hashes(),
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
def test_find_pending_hand_starts_carries_the_previous_real_attempt_message():
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    gate = "failed_transient: P5-5: all_in_mismatch: preflop action 3 BB all_in 9.28"
    try:
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient", gate)

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert len(results) == 1
        assert results[0].previous_status_message == gate
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_skips_a_mark_when_finding_the_previous_attempt():
    """A mark is a deliberate reprocess, not an outcome. If it stood in for the
    previous real attempt, a mark between two identical gate failures would hide
    the repeat — which is exactly the sequence 004_001 and 014_003 both have.

    The mark is written through mark_pending's own prefix, so this exercises the
    binding between the message and the pattern, not a copy of either.
    """
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")
    gate = "failed_transient: P5-5: all_in_mismatch: preflop action 3 BB all_in 9.28"
    try:
        _write_hand_start_attempt(bq_client, ids.hand_start_id, "failed_transient", gate)
        _write_hand_start_attempt(
            bq_client, ids.hand_start_id, "failed_transient",
            f"{MARK_MESSAGE_PREFIX}hand_actions",
        )

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )
        assert len(results) == 1
        # The mark is the latest row, and the gate failure is still what counts.
        assert results[0].previous_status_message == gate
        # And it costs no retry slot: an old-style mark is recognised by its
        # message and resets the count, so the gate failure before it is no
        # longer counted either.
        assert results[0].consecutive_failures == 0
    finally:
        _cleanup_hand_start(bq_client, ids)


@pytest.mark.integration
def test_find_pending_hand_starts_has_no_previous_message_before_any_attempt():
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
        assert results[0].previous_status_message is None
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


# A mark must leave the entity eligible with a clean retry budget whatever
# preceded it, and marking twice must cost nothing. Written through
# mark_pending's own status and message, so a rename of either moves these
# tests with it rather than leaving them asserting a stale literal.
_PENDING_MARK = (MARK_STATUS, f"{MARK_MESSAGE_PREFIX}hand_actions")
# A mark written before `marked_pending` existed: a `failed_transient` row
# carrying the mark message. Over a thousand are in the corpus, and the counter
# has to recognise them by message or they keep spending a retry slot.
_PENDING_OLD_MARK = ("failed_transient", f"{MARK_MESSAGE_PREFIX}hand_actions")

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
def test_find_pending_hand_starts_gives_a_marked_hand_a_clean_retry_budget(
    history, expected_failures
):
    from google.cloud import bigquery as bq

    bq_client = bq.Client(project=_INTEGRATION_PROJECT)
    ids = _seed_hand_start(bq_client, uid_tag="p5q")

    try:
        for entry in history:
            status, message = entry if isinstance(entry, tuple) else (entry, None)
            _write_hand_start_attempt(bq_client, ids.hand_start_id, status, message)

        results = _find_pending_hand_starts(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_hand_start_ids=[ids.hand_start_id],
            client=bq_client,
        )

        assert len(results) == 1, f"Expected the marked hand to be selected, got {results}"
        assert results[0].consecutive_failures == expected_failures
    finally:
        _cleanup_hand_start(bq_client, ids)


# ---------------------------------------------------------------------------
# Provenance
#
# Step E's prompts and reference images are listed only when step E ran. A hand
# that ended preflop makes no scan and no read, so naming those files would
# claim a provenance the row does not have.
# ---------------------------------------------------------------------------


def _provenance_from(mocks):
    return _written_row(mocks).hand_action_state["provenance"]


def test_preflop_only_hand_lists_step_d_prompt_only():
    with _patched([_d_result()]) as mocks:
        _call(_pending())

    provenance = _provenance_from(mocks)
    assert set(provenance["prompts"]) == {"prompts/extract_player_actions.md"}
    # No frame call was made, so no frame model is claimed.
    assert set(provenance["models"]) == {"clip"}


def test_hand_reaching_the_flop_lists_step_e_prompts_and_references():
    with _patched(
        [_d_result(("preflop", "flop")), _scan()],
        frame_results=[{"new_cards": ["Ah", "Kd", "2c"]}],
    ) as mocks:
        _call(_pending())

    provenance = _provenance_from(mocks)
    assert set(provenance["prompts"]) == {
        "prompts/extract_player_actions.md",
        "prompts/identify_community_cards.md",
        "prompts/extract_community_cards.md",
        "references/flop_reference.jpeg",
        "references/turn_reference.jpeg",
        "references/river_reference.jpeg",
    }
    # The card read is a frame-mode call; the scan is clip-mode.
    assert set(provenance["models"]) == {"clip", "frame"}


def test_provenance_records_media_resolution_per_call_mode():
    """Step E's frame read is ULTRA_HIGH; the scans are clip-mode and unset.

    Resolution changes what a read returns while leaving the model id and every
    prompt hash untouched, so without this a corpus read at two resolutions is
    indistinguishable in the data. Keys must match `models` exactly.
    """
    with _patched(
        [_d_result(("preflop", "flop")), _scan()],
        frame_results=[{"new_cards": ["Ah", "Kd", "2c"]}],
    ) as mocks:
        _call(_pending())

    provenance = _provenance_from(mocks)
    assert provenance["media_resolution"] == {
        "clip": CLIP_MEDIA_RESOLUTION,
        "frame": FRAME_RESOLUTION_ULTRA_HIGH,
    }
    assert set(provenance["media_resolution"]) == set(provenance["models"])


def test_preflop_only_hand_records_no_frame_resolution():
    """Step E never ran, so there is no frame call to describe.

    Same rule as the step-E prompts and references: naming a call mode the row
    did not use would make the record say something false about how it was made.
    """
    with _patched([_d_result(("preflop",))]) as mocks:
        _call(_pending())

    provenance = _provenance_from(mocks)
    assert provenance["media_resolution"] == {"clip": CLIP_MEDIA_RESOLUTION}
    assert set(provenance["media_resolution"]) == set(provenance["models"])


def test_all_three_references_are_listed_even_for_a_flop_only_hand():
    """Every scan carries all three images, so all three contributed."""
    with _patched(
        [_d_result(("preflop", "flop")), _scan()],
        frame_results=[{"new_cards": ["Ah", "Kd", "2c"]}],
    ) as mocks:
        _call(_pending())

    prompts = _provenance_from(mocks)["prompts"]
    assert "references/turn_reference.jpeg" in prompts
    assert "references/river_reference.jpeg" in prompts


def test_provenance_hashes_are_the_ones_handed_in():
    with _patched([_d_result()]) as mocks:
        _call(_pending())

    prompts = _provenance_from(mocks)["prompts"]
    assert prompts["prompts/extract_player_actions.md"] == (
        _P5_HASHES["prompts/extract_player_actions.md"]
    )


# ---------------------------------------------------------------------------
# Step-D gates (P5-1 .. P5-9, P5-15)
# ---------------------------------------------------------------------------

_GATE_SETUP = {
    "total_seat_count": 3,
    "pot_size_bb": 1.5,
    "players": [
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 40.0},
        {"seat_number": 2, "seat_position_label": "SB", "stack_size": 30.0},
        {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 20.0},
    ],
}
_GATE_FVA = {
    "seat_position_label": "BTN", "seat_number": 3,
    "action_type": "raise", "bet_amount": 2.5,
}


def _acts(*rows):
    return [
        {"action_order": i, "seat_position_label": lb, "action_type": t, "bet_amount": amt}
        for i, (lb, t, amt) in enumerate(rows, start=1)
    ]


def _gate(streets, winners=("BTN",), setup=None, fva=None):
    return check_step_d_output(
        setup or _GATE_SETUP, fva or _GATE_FVA, streets, list(winners)
    )


def _clean_preflop():
    return {
        "street_name": "preflop",
        "actions": _acts(("BTN", "raise", 2.5), ("SB", "fold", 0.0), ("BB", "call", 2.5)),
    }


def test_a_clean_hand_passes_every_gate():
    assert _gate([_clean_preflop()], winners=("BB",)) is None


# --- P5-7c scoping: the case that forced it ------------------------------


def test_a_called_all_in_runout_passes_even_though_the_caller_never_acts():
    """The case P5-7c must not fire on, and the reason it is scoped to live
    streets.

    BB calls BTN's shove and is the deeper stack, so it still holds chips on
    the flop and turn — and correctly never acts, because betting closed
    preflop. Unscoped, "every seat still in with chips must act" would fail
    this, and it measured 9 of 133 corpus hands.
    """
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "all_in", 20.0), ("SB", "fold", 0.0), ("BB", "call", 20.0))},
        {"street_name": "flop", "actions": []},
        {"street_name": "turn", "actions": []},
        {"street_name": "river", "actions": []},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 20.0}
    assert _gate(streets, winners=("BB",), fva=fva) is None


def test_an_empty_street_on_a_live_board_fails_p5_7c():
    """The mirror image. Nobody is all-in, both seats have chips, and the flop
    carries no actions — somebody had to act, even if only to check."""
    streets = [
        _clean_preflop(),
        {"street_name": "flop", "actions": []},
        {"street_name": "turn",
         "actions": _acts(("BB", "check", 0.0), ("BTN", "check", 0.0))},
    ]
    reason = _gate(streets, winners=("BB",))
    assert reason.startswith("P5-7: illegal_betting: (c) flop ")
    assert "BB" in reason and "BTN" in reason


def test_a_seat_that_never_acts_on_a_live_street_fails_p5_7c():
    """The measured defect: step D omitted one seat's fold, so it reads as
    still in with chips for the rest of the hand. Caught at preflop, the street
    that actually went wrong."""
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "raise", 2.5), ("BB", "call", 2.5))},   # SB never acts
        {"street_name": "flop",
         "actions": _acts(("BB", "check", 0.0), ("BTN", "check", 0.0))},
    ]
    reason = _gate(streets, winners=("BB",))
    assert reason.startswith("P5-7: illegal_betting: (c) preflop ")
    assert "SB" in reason


# --- P5-1, P5-3, P5-4, P5-15: structural ---------------------------------


def test_p5_1_an_action_by_a_seat_not_in_the_hand():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("CO", "call", 2.5))}]
    assert _gate(streets).startswith("P5-1: action_label_unresolved: ")


def test_p5_3_a_street_after_the_hand_ended_by_folds():
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "raise", 2.5), ("SB", "fold", 0.0), ("BB", "fold", 0.0))},
        {"street_name": "flop", "actions": []},
    ]
    assert _gate(streets).startswith("P5-3: action_after_hand_end: ")


def test_p5_4_an_action_by_a_seat_that_already_folded():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "fold", 0.0),
                                 ("SB", "call", 2.5))}]
    assert _gate(streets).startswith("P5-4: action_after_fold_or_all_in: ")


def test_p5_15_an_unrecognised_street_name():
    streets = [_clean_preflop(), {"street_name": "turn2", "actions": []}]
    assert _gate(streets, winners=("BB",)).startswith("P5-15: unknown_street: ")


def test_street_names_and_action_types_are_normalized_before_comparison():
    """A capitalised Turn from step D must not read as an unknown street, and a
    RAISE must not read as an unknown action type. Both would fail a correct
    hand on formatting alone."""
    streets = [
        {"street_name": "  PreFlop ",
         "actions": [
             {"action_order": 1, "seat_position_label": "BTN",
              "action_type": "RAISE", "bet_amount": 2.5},
             {"action_order": 2, "seat_position_label": "SB",
              "action_type": "Fold", "bet_amount": 0.0},
             {"action_order": 3, "seat_position_label": "BB",
              "action_type": "Call", "bet_amount": 2.5},
         ]},
        {"street_name": "FLOP",
         "actions": _acts(("BB", "check", 0.0), ("BTN", "check", 0.0))},
    ]
    assert _gate(streets, winners=("BB",)) is None


# --- P5-9 amount vs type --------------------------------------------------


@pytest.mark.parametrize("action_type", ["fold", "check"])
def test_p5_9_a_fold_or_check_carrying_chips(action_type):
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", action_type, 3.0))}]
    assert _gate(streets).startswith("P5-9: amount_type_mismatch: ")


def test_p5_9_a_commitment_of_zero():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "call", 0.0))}]
    assert _gate(streets).startswith("P5-9: amount_type_mismatch: ")


def test_p5_9_a_negative_amount():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "fold", -1.0))}]
    assert _gate(streets).startswith("P5-9: amount_type_mismatch: ")


# --- P5-5 all-in -----------------------------------------------------------


def test_p5_5_an_all_in_that_leaves_chips_behind():
    """The transposed pair: a shove recorded where a raise belongs."""
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "all_in", 5.0), ("SB", "fold", 0.0),
                                 ("BB", "fold", 0.0))}]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 5.0}
    reason = _gate(streets, fva=fva)
    assert reason.startswith("P5-5: all_in_mismatch: ")
    assert "leaves" in reason


def test_p5_5_a_transposed_raise_and_all_in_pair_is_still_caught():
    """The case the removed reverse check was added for. ARCHITECTURE's t=584
    hand with its two amounts swapped: the truth is a raise to 7 preflop and a
    4.55 shove on the flop, so a transposition labels the *smaller* flop action
    all_in, where it leaves 0.5 behind. The forward branch has it — a swap always
    puts the all_in label on the action that does not exhaust the stack."""
    setup = {
        "total_seat_count": 3,
        "pot_size_bb": 1.5,
        "players": [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 20.0},
            {"seat_number": 2, "seat_position_label": "SB", "stack_size": 11.1},
            {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 15.0},
        ],
    }
    fva = {"seat_position_label": "SB", "seat_number": 2,
           "action_type": "raise", "bet_amount": 4.55}
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("SB", "raise", 4.55), ("BB", "call", 4.55))},
        {"street_name": "flop",
         "actions": _acts(("SB", "all_in", 6.55), ("BB", "call", 6.55))},
    ]
    reason = _gate(streets, winners=("SB",), setup=setup, fva=fva)
    assert reason.startswith("P5-5: all_in_mismatch: ")
    assert "leaves" in reason


def test_p5_5_a_whole_stack_call_recorded_as_call_is_accepted():
    """YzKyFMQ1avU_013_003, the hand the reverse check failed. The BB calls a
    shove with exactly its stack — 7.03 behind plus its 1 BB blind — and the
    broadcast's own label changes from "Call" to "All-in" as it lands. Either
    label is a correct reading, so the sequence must pass."""
    setup = {
        "total_seat_count": 3,
        "pot_size_bb": 1.5,
        "players": [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 7.03},
            {"seat_number": 2, "seat_position_label": "SB", "stack_size": 20.0},
            {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 15.0},
        ],
    }
    fva = {"seat_position_label": "SB", "seat_number": 2,
           "action_type": "all_in", "bet_amount": 20.5}
    streets = [{"street_name": "preflop",
                "actions": _acts(("SB", "all_in", 20.5), ("BB", "call", 8.03))}]
    assert _gate(streets, winners=("SB",), setup=setup, fva=fva) is None


def test_p5_5_committing_more_than_the_stack():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 70.0), ("SB", "fold", 0.0),
                                 ("BB", "fold", 0.0))}]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "raise", "bet_amount": 70.0}
    reason = _gate(streets, fva=fva)
    assert reason.startswith("P5-5: all_in_mismatch: ")
    assert "beyond its stack" in reason


# --- P5-7a betting legality ------------------------------------------------


def test_p5_7a_a_check_while_owing():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "check", 0.0))}]
    assert _gate(streets).startswith("P5-7: illegal_betting: (a) ")


def test_p5_7a_a_raise_that_does_not_raise():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "raise", 2.5))}]
    assert _gate(streets).startswith("P5-7: illegal_betting: (a) ")


def test_p5_7a_a_bet_preflop():
    """A bet is never legal preflop: the BB's post means something is owed."""
    streets = [{"street_name": "preflop", "actions": _acts(("BTN", "bet", 2.5))}]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "bet", "bet_amount": 2.5}
    assert _gate(streets, fva=fva).startswith("P5-7: illegal_betting: (a) ")


def test_p5_7a_a_short_call_is_legal_when_it_is_all_in_for_less():
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "raise", 20.0), ("SB", "fold", 0.0), ("BB", "call", 20.0))},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 20.0}
    # BTN's 20.0 exhausts its 20.0 stack, so it must be recorded all_in.
    streets[0]["actions"][0]["action_type"] = "all_in"
    assert _gate(streets, winners=("BB",), fva=fva) is None


def test_p5_7a_the_bb_may_check_preflop_after_a_limp():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "call", 1.0), ("SB", "fold", 0.0),
                                 ("BB", "check", 0.0))},
               {"street_name": "flop",
                "actions": _acts(("BB", "check", 0.0), ("BTN", "check", 0.0))}]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "call", "bet_amount": 1.0}
    assert _gate(streets, winners=("BB",), fva=fva) is None


# --- P5-8 FVA cross-check --------------------------------------------------


def test_p5_8_the_documented_pre_fva_fold():
    """The adjudicated case: the fva block reads SB call 1, and step D opens
    with BTN fold anyway."""
    setup = {
        "total_seat_count": 3, "pot_size_bb": 1.5,
        "players": [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 40.0},
            {"seat_number": 2, "seat_position_label": "SB", "stack_size": 30.0},
            {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 20.0},
        ],
    }
    fva = {"seat_position_label": "SB", "seat_number": 2,
           "action_type": "call", "bet_amount": 1.0}
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "fold", 0.0), ("SB", "call", 1.0),
                                 ("BB", "check", 0.0))}]
    reason = check_step_d_output(setup, fva, streets, ["SB"])
    # Caught by P5-4 rather than P5-8, and that is the better diagnosis. The BTN
    # sits above the FVA seat in preflop acting order, so build_seats has it
    # already folded before the sequence starts — the message says the seat
    # cannot act at all, rather than that two records disagree. P5-8 would catch
    # it too; the structural gate simply runs first.
    assert reason.startswith("P5-4: action_after_fold_or_all_in: ")
    assert "BTN" in reason


def test_p5_8_a_disagreeing_amount():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 4.0), ("SB", "fold", 0.0),
                                 ("BB", "fold", 0.0))}]
    assert _gate(streets).startswith("P5-8: fva_mismatch: ")


def test_p5_8_tolerates_a_rounding_difference():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.55), ("SB", "fold", 0.0),
                                 ("BB", "fold", 0.0))}]
    assert _gate(streets) is None


# --- P5-2 and P5-6 winners -------------------------------------------------


def test_p5_2_a_winner_that_is_not_a_seat_in_the_hand():
    assert _gate([_clean_preflop()], winners=("CO",)).startswith(
        "P5-2: winner_unresolved: "
    )


def test_p5_2_a_repeated_winner():
    assert _gate([_clean_preflop()], winners=("BB", "BB")).startswith(
        "P5-2: winner_unresolved: "
    )


def test_p5_6_a_winner_that_folded():
    assert _gate([_clean_preflop()], winners=("SB",)).startswith(
        "P5-6: implausible_winner: "
    )


def test_p5_6_folds_leaving_one_seat_means_that_seat_won():
    streets = [{"street_name": "preflop",
                "actions": _acts(("BTN", "raise", 2.5), ("SB", "fold", 0.0),
                                 ("BB", "fold", 0.0))}]
    assert _gate(streets, winners=("BTN",)) is None
    assert _gate(streets, winners=("BB",)).startswith("P5-6: implausible_winner: ")


def test_p5_6_a_split_pot_is_allowed_at_showdown():
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "all_in", 20.0), ("SB", "fold", 0.0), ("BB", "call", 20.0))},
        {"street_name": "flop", "actions": []},
        {"street_name": "turn", "actions": []},
        {"street_name": "river", "actions": []},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 20.0}
    assert _gate(streets, winners=("BB", "BTN"), fva=fva) is None


# --- ordering and cost -----------------------------------------------------


def test_the_gate_runs_before_any_step_e_call():
    """The whole point of gating here: a failing hand costs one clip call
    rather than up to seven."""
    bad = _d_result(actions=[
        {"action_order": 1, "seat_position_label": "HJ",   # not a seat at this table
         "action_type": "raise", "bet_amount": 2.5},
    ])
    with _patched([bad]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert mocks.clip.call_count == 1      # step D only
    mocks.frame.assert_not_called()
    mocks.write_actions.assert_not_called()
    assert "P5-1: action_label_unresolved: " in _attempt_row(mocks).status_message


def test_a_gate_failure_parks_at_the_cap():
    bad = _d_result(actions=[
        {"action_order": 1, "seat_position_label": "HJ",   # not a seat at this table
         "action_type": "raise", "bet_amount": 2.5},
    ])
    with _patched([bad]) as mocks:
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"
    assert "P5-1: action_label_unresolved: " in _attempt_row(mocks).status_message


# ---------------------------------------------------------------------------
# An identical gate repeat is permanent
#
# A step-D gate failure that reproduces the previous real attempt's message word
# for word is a defect in the input, not a stochastic step-D slip. No retry of
# this phase can reach it, and each one costs a step-D call on Pro.
# ---------------------------------------------------------------------------


_P5_5_REASON = (
    "P5-5: all_in_mismatch: preflop action 3 BB all_in 9.28 leaves 10.00 BB behind"
)
_P5_8_REASON = (
    "P5-8: fva_mismatch: preflop action 1 commits 17.6, but the fva block says 17.1"
)
_INFRA_MESSAGE = "rate limited by Vertex AI (429); retries exhausted"


@pytest.mark.parametrize("previous,repeats", [
    # YzKyFMQ1avU_004_001, which recorded this three times running.
    (f"failed_transient: {_P5_5_REASON}", True),
    # A predecessor stored as failed_parked: the hand was un-parked by a mark, and
    # the gate text is still the same text.
    (f"failed_parked: {_P5_5_REASON}", True),
    # Same gate, different detail — a stochastic slip, which is what varies.
    ("failed_transient: P5-5: all_in_mismatch: preflop action 2 SB all_in 4.0 "
     "leaves 1.00 BB behind", False),
    (f"failed_transient: {_P5_8_REASON}", False),
    (_INFRA_MESSAGE, False),
    ("complete: window=47s streets=preflop,flop,turn,river", False),
    (None, False),
])
def test_only_an_identical_gate_message_counts_as_a_repeat(previous, repeats):
    assert _repeats_previous_gate_failure(_P5_5_REASON, previous) is repeats


def test_an_identical_repeat_is_permanent_and_says_why():
    status, message = _gate_failure_outcome(
        _P5_5_REASON, f"failed_transient: {_P5_5_REASON}", 0, 3
    )
    assert status == "failed_permanent"
    # The gate prefix survives, or the per-gate report loses this hand's history.
    assert _P5_5_REASON in message
    assert "repeated identically" in message


def test_an_identical_repeat_outranks_the_retry_cap():
    """Both are terminal; this one carries the diagnosis."""
    status, message = _gate_failure_outcome(
        _P5_5_REASON, f"failed_transient: {_P5_5_REASON}", 2, 3
    )
    assert status == "failed_permanent"
    assert "repeated identically" in message


@pytest.mark.parametrize("previous", [None, _INFRA_MESSAGE, f"failed_transient: {_P5_8_REASON}"])
def test_a_non_repeat_keeps_the_existing_transient_treatment(previous):
    status, message = _gate_failure_outcome(_P5_5_REASON, previous, 0, 3)
    assert status == "failed_transient"
    assert message == f"failed_transient: {_P5_5_REASON}"


@pytest.mark.parametrize("previous", [None, _INFRA_MESSAGE])
def test_a_non_repeat_still_parks_at_the_cap(previous):
    status, _ = _gate_failure_outcome(_P5_5_REASON, previous, 2, 3)
    assert status == "failed_parked"


def test_an_infrastructure_message_never_matches_however_often_it_repeats():
    """429s, 5xx and connection resets legitimately repeat, and none of them is a
    judgement about the hand. The gate-id prefix is what draws the line."""
    assert _gate_failure_outcome(_P5_5_REASON, _INFRA_MESSAGE, 0, 3)[0] == "failed_transient"
    assert _repeats_previous_gate_failure(_INFRA_MESSAGE, _INFRA_MESSAGE) is False


def test_p5_14_is_out_of_scope_even_though_it_is_a_p5_gate():
    """Its message names only a street, so a stochastic scan miss repeats
    byte-identically — and Pro recovered 5 of 7 contested truncations on retry.
    P5-14 writes its own status and never reaches _gate_failure_outcome; this
    pins the decision rather than the plumbing."""
    truncation = (
        "P5-14: contested_street_unread: D reported turn but 2 scans found none, "
        "and betting was still live there"
    )
    with _patched([_d_result()]) as mocks:   # a hand that does not reach P5-14
        _call(_pending(previous_status_message=f"failed_transient: {truncation}"))
    assert mocks.write_actions.call_count == 1


def test_the_repeat_rule_fires_through_the_orchestrator():
    """004_001's shape: the same gate message twice, so the second attempt ends
    the hand instead of buying a third Pro call."""
    bad = _d_result(actions=[
        {"action_order": 1, "seat_position_label": "HJ",   # not a seat at this table
         "action_type": "raise", "bet_amount": 2.5},
    ])
    reason = ("P5-1: action_label_unresolved: preflop action 1 names seat 'HJ', "
              "which is not in this hand")
    with _patched([bad]) as mocks:
        outcome = _call(_pending(previous_status_message=f"failed_transient: {reason}"))

    assert outcome == "failed_permanent"
    assert mocks.clip.call_count == 1      # step D, and nothing after it
    mocks.write_actions.assert_not_called()
    row = _attempt_row(mocks)
    assert row.status == "failed_permanent"
    assert row.status_message.startswith(f"failed_permanent: {reason}")
    assert "repeated identically" in row.status_message


def test_a_mark_pending_row_is_not_the_previous_real_attempt():
    """The pending query is what skips marks, so what this pins is the other
    half: the prefix Phase 5 matches on is the message mark_pending writes. A
    mark between two identical failures must not hide the repeat.

    One definition, two readers — if they diverge, a mark counts as a real
    attempt and a first genuine failure reads as a repeat.
    """
    for stage in ("tournament_results", "clip_manifest", "hand_setups",
                  "hand_starts", "hand_actions"):
        mark = f"{MARK_MESSAGE_PREFIX}{stage}"
        assert mark.startswith(MARK_MESSAGE_PREFIX)
        # And it is not mistakable for a gate failure or an outage.
        assert _repeats_previous_gate_failure(_P5_5_REASON, mark) is False


# ---------------------------------------------------------------------------
# P5-10 / D3 — the inert-street skip
# ---------------------------------------------------------------------------


def _runout_pending():
    """A heads-up hand whose preflop all-in is called, so flop, turn and river
    are all inert."""
    state = _hand_start_state(
        total_seat_count=2,
        fva={"seat_position_label": "BTN", "seat_number": 3,
             "action_type": "all_in", "bet_amount": 80.5},
        players=[
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
             "hole_cards": ["Ah", "Kd"]},
            {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 80.0,
             "hole_cards": ["2c", "3c"]},
        ],
    )
    d_result = _d_result(
        street_names=("preflop", "flop", "turn", "river"),
        winning_positions=("BB",),
        actions=[
            {"action_order": 1, "seat_position_label": "BTN",
             "action_type": "all_in", "bet_amount": 80.5},
            {"action_order": 2, "seat_position_label": "BB",
             "action_type": "call", "bet_amount": 80.5},
        ],
        postflop_actions=[],
    )
    return _pending(hand_start_state=state), d_result


def test_an_inert_street_makes_no_scan_and_no_frame_read():
    pending, d_result = _runout_pending()
    with _patched([d_result]) as mocks:
        outcome = _call(pending)

    assert outcome == "complete"
    assert mocks.clip.call_count == 1      # step D only — no scans at all
    mocks.frame.assert_not_called()
    mocks.extract.assert_not_called()
    mocks.upload.assert_not_called()


def test_an_inert_street_is_recorded_with_no_cards_and_no_timestamp():
    pending, d_result = _runout_pending()
    with _patched([d_result]) as mocks:
        _call(pending)

    streets = _written_row(mocks).hand_action_state["streets"]
    by_name = {s["street_name"]: s for s in streets}
    assert [s["street_name"] for s in streets] == ["preflop", "flop", "turn", "river"]
    for name in ("flop", "turn", "river"):
        assert by_name[name]["extraction_status"] == "skipped_inert"
        assert by_name[name]["community_cards"] == []
        assert by_name[name]["street_timestamp"] is None


def test_an_inert_street_uploads_no_frame():
    pending, d_result = _runout_pending()
    with _patched([d_result]) as mocks:
        _call(pending)
    assert _written_row(mocks).street_frame_gcs_paths == []


def test_preflop_is_recorded_not_applicable():
    with _patched([_d_result()]) as mocks:
        _call(_pending())
    streets = _written_row(mocks).hand_action_state["streets"]
    assert streets[0]["street_name"] == "preflop"
    assert streets[0]["extraction_status"] == "not_applicable"


def test_a_live_street_is_scanned_and_recorded_extracted():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [{"new_cards": ["5d", "8d", "As"]}]
    with _patched(clip_results, frame_results) as mocks:
        _call(_pending())

    streets = _written_row(mocks).hand_action_state["streets"]
    flop = next(s for s in streets if s["street_name"] == "flop")
    assert flop["extraction_status"] == "extracted"
    assert flop["community_cards"] == ["5d", "8d", "As"]
    assert flop["street_timestamp"] == 120


def test_the_inert_skip_keeps_the_actions_d_reported():
    """Skipping the scan must not discard the street's action array — a runout
    street legitimately has none, but the key stays."""
    pending, d_result = _runout_pending()
    with _patched([d_result]) as mocks:
        _call(pending)
    streets = _written_row(mocks).hand_action_state["streets"]
    assert all("actions" in s for s in streets)


def test_every_street_carries_an_extraction_status():
    pending, d_result = _runout_pending()
    with _patched([d_result]) as mocks:
        _call(pending)
    streets = _written_row(mocks).hand_action_state["streets"]
    assert all(s["extraction_status"] for s in streets)
    assert "unread" not in {s["extraction_status"] for s in streets}


# ---------------------------------------------------------------------------
# P5-12 — duplicate board card
# ---------------------------------------------------------------------------


def test_p5_12_a_card_repeated_on_the_board():
    assert _board_duplicate(["Ah", "Kd", "2c"], ["Ah"]).startswith(
        "P5-12: duplicate_board_card: "
    )


def test_p5_12_a_card_repeated_within_one_read():
    assert _board_duplicate([], ["Ah", "Ah", "2c"]) is not None


def test_p5_12_compares_case_folded():
    """normalize_card maps '10' to 'T' and nothing else, so a naive comparison
    would let this through."""
    assert _board_duplicate(["Ah", "Kd", "2c"], ["AH"]) is not None


def test_p5_12_a_clean_board_passes():
    assert _board_duplicate(["Ah", "Kd", "2c"], ["Ts"]) is None


def test_p5_12_ignores_nulls():
    """A null is the card-count check's business, not this one's."""
    assert _board_duplicate(["Ah", None], [None]) is None


def test_p5_12_a_duplicate_exhausts_the_read_attempts_and_fails_transient():
    clip_results = [_d_result(street_names=("preflop", "flop")), _scan(timestamp="02:00")]
    frame_results = [{"new_cards": ["5d", "5d", "As"]}] * CARD_READ_ATTEMPTS
    with _patched(clip_results, frame_results) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert mocks.frame.call_count == CARD_READ_ATTEMPTS
    assert "P5-12: duplicate_board_card: " in _attempt_row(mocks).status_message


# ---------------------------------------------------------------------------
# P5-16 — missing hole cards on a seat that stayed in (H5)
# ---------------------------------------------------------------------------

_P5_16_CARDS = {"BB": ["Ah", "Kd"], "SB": ["Qs", "Jh"], "BTN": ["2c", "3c"]}


def _cards_setup(*unreadable, stacks=None):
    """_GATE_SETUP with hole cards on every seat, null on the ones named."""
    return {
        **_GATE_SETUP,
        "players": [
            {
                **player,
                "stack_size": (stacks or {}).get(
                    player["seat_position_label"], player["stack_size"]
                ),
                "hole_cards": (
                    None
                    if player["seat_position_label"] in unreadable
                    else _P5_16_CARDS[player["seat_position_label"]]
                ),
            }
            for player in _GATE_SETUP["players"]
        ],
    }


def _missing(streets, *unreadable, fva=None, stacks=None):
    return check_missing_hole_cards(
        _cards_setup(*unreadable, stacks=stacks), fva or _GATE_FVA, streets
    )


def test_p5_16_a_fold_only_seat_may_have_null_cards():
    """The whole point of the rule. SB's first and only action after the FVA is
    a fold, so it mucked and the FVA frame correctly shows it no cards — the
    hand still carries its whole story."""
    assert _missing([_clean_preflop()], "SB") is None


def test_p5_16_a_seat_that_calls_with_null_cards_fails():
    """BB calls the FVA raise and is in for the rest of the hand."""
    reason = _missing([_clean_preflop()], "BB")
    assert reason.startswith("P5-16: missing_hole_cards_live_seat: ")
    assert "BB" in reason


def test_p5_16_a_seat_that_checks_with_null_cards_fails():
    """A check commits no chips but keeps the seat's cards live, which is what
    makes them a defect."""
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "call", 1.0), ("SB", "fold", 0.0),
                          ("BB", "check", 0.0))},
        {"street_name": "flop",
         "actions": _acts(("BB", "check", 0.0), ("BTN", "check", 0.0))},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "call", "bet_amount": 1.0}
    reason = _missing(streets, "BB", fva=fva)
    assert reason.startswith("P5-16: missing_hole_cards_live_seat: ")
    assert "BB" in reason


def test_p5_16_a_seat_that_reaches_showdown_with_null_cards_fails():
    """A called all-in runout: BB is still in when the actions run out, so its
    cards are shown and read at the end."""
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "all_in", 20.0), ("SB", "fold", 0.0),
                          ("BB", "call", 20.0))},
        {"street_name": "flop", "actions": []},
        {"street_name": "turn", "actions": []},
        {"street_name": "river", "actions": []},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 20.0}
    assert _missing(streets, "BB", fva=fva) is not None


def test_p5_16_a_blind_all_in_from_its_post_with_null_cards_fails():
    """It never acts at all — it cannot — but it is in the hand and its cards
    are live, so a null there is a read failure like any other."""
    streets = [
        {"street_name": "preflop",
         "actions": _acts(("BTN", "all_in", 20.0), ("SB", "fold", 0.0))},
        {"street_name": "flop", "actions": []},
        {"street_name": "turn", "actions": []},
        {"street_name": "river", "actions": []},
    ]
    fva = {"seat_position_label": "BTN", "seat_number": 3,
           "action_type": "all_in", "bet_amount": 20.0}
    reason = _missing(streets, "BB", fva=fva, stacks={"BB": 0.0})
    assert reason.startswith("P5-16: missing_hole_cards_live_seat: ")
    assert "BB" in reason


def test_p5_16_leaves_the_fva_seat_to_p4_6():
    """One gate per seat. P4-6 already failed the hand permanently if the FVA's
    own cards did not read, so a hand that reaches Phase 5 cannot have this —
    and reporting it here would give the same defect two ids."""
    assert _missing([_clean_preflop()], "BTN") is None


def test_p5_16_a_pre_fva_fold_may_have_null_cards():
    """A seat above the FVA in preflop acting order folded before the FVA frame
    was taken, so its cards were never read and must not be demanded."""
    fva = {"seat_position_label": "SB", "seat_number": 2,
           "action_type": "raise", "bet_amount": 2.5}
    streets = [{"street_name": "preflop",
                "actions": _acts(("SB", "raise", 2.5), ("BB", "call", 2.5))}]
    assert _missing(streets, "BTN", fva=fva) is None


def test_p5_16_a_hand_with_every_seat_readable_passes():
    assert _missing([_clean_preflop()]) is None


def test_p5_16_fails_permanent_after_step_d_and_before_step_e():
    """At the orchestrator: one clip call spent, no frame read, no row."""
    players = [
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0,
         "hole_cards": [None, None]},
        {"seat_number": 4, "seat_position_label": "CO", "stack_size": 80.0,
         "hole_cards": ["2c", "3c"]},
    ]
    hs = _pending(hand_start_state=_hand_start_state(players=players))
    with _patched([_d_result(street_names=("preflop", "flop"))]) as mocks:
        outcome = _call(hs)

    assert outcome == "failed_permanent"
    mocks.write_actions.assert_not_called()
    assert mocks.clip.call_count == 1
    assert mocks.frame.call_count == 0

    row = _attempt_row(mocks)
    assert row.status == "failed_permanent"
    assert row.status_message.startswith("P5-16: missing_hole_cards_live_seat: ")
    assert "BB" in row.status_message
