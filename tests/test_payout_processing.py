import asyncio
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from table_talk.gemini_caller import GeminiPermanentError, GeminiTransientError
from table_talk.payout_processing import (
    FRAME_FALLBACK_LADDER,
    MIN_LADDER_RANKS,
    PendingVideo,
    _derive_bounty_type,
    _find_pending_videos,
    _normalize_panel,
    _parse_amount,
    _transient_status,
    check_preconditions,
    process_pending_videos,
    process_video,
)
from table_talk.videos_downloader import DownloadPermanentError

_PROMPT = "RESULTS PANEL PROMPT"
_BUCKET = "tournament-results-bucket"
_LONG_ENOUGH = 3000


def _panel(**overrides):
    panel = {
        "panel_visible": True,
        "has_bounty_column": False,
        "currency_symbol": "$",
        "rows": [
            {"rank": 1, "payout": 62760.03, "payout_marked": True, "bounty": None},
            {"rank": 2, "payout": 54407.04, "payout_marked": True, "bounty": None},
            {"rank": 3, "payout": 49632.96, "payout_marked": True, "bounty": None},
            {"rank": 4, "payout": 26271.31, "payout_marked": False, "bounty": None},
            {"rank": 5, "payout": 18436.34, "payout_marked": False, "bounty": None},
        ],
    }
    panel.update(overrides)
    return panel


def _not_visible():
    return {
        "panel_visible": False,
        "has_bounty_column": None,
        "currency_symbol": None,
        "rows": [],
    }


def _pending(**kwargs) -> PendingVideo:
    defaults = {
        "video_id": "dQw4w9WgXcQ",
        "duration_seconds": _LONG_ENOUGH,
        "consecutive_failures": 0,
    }
    return PendingVideo(**{**defaults, **kwargs})


def _fake_extract_frame(_video_uri, _ts, output_path):
    with open(output_path, "wb") as f:
        f.write(b"\xff\xd8\xff\x00" * 4)


def _mock_bq_client(rows=None):
    client = MagicMock()
    client.query.return_value.result.return_value = rows or []
    return client


def _bq_row(**kwargs):
    return SimpleNamespace(**kwargs)


def _run(coro):
    return asyncio.run(coro)


@contextmanager
def _patched(frame_results):
    """Patch every collaborator process_video reaches.

    frame_results is passed to side_effect, so it may be a list of panels (one
    per ladder rung) or an exception.
    """
    with (
        patch(
            "table_talk.payout_processing.call_gemini_for_frame",
            side_effect=frame_results,
        ) as frame,
        patch(
            "table_talk.payout_processing.extract_frame",
            side_effect=_fake_extract_frame,
        ) as extract,
        patch("table_talk.payout_processing.upload_frame") as upload,
        patch("table_talk.payout_processing.write_tournament_results") as write_results,
        patch(
            "table_talk.payout_processing.write_tournament_results_processing_attempt_row"
        ) as write_attempt,
    ):
        yield SimpleNamespace(
            frame=frame,
            extract=extract,
            upload=upload,
            write_results=write_results,
            write_attempt=write_attempt,
        )


def _call(video, mocks_max_attempts=3):
    return _run(
        process_video(
            video,
            "/tmp/fake.mp4",
            "proj",
            "ds",
            _BUCKET,
            _PROMPT,
            max_attempts=mocks_max_attempts,
        )
    )


def _attempt_row(mocks):
    return mocks.write_attempt.call_args[0][0]


def _written_row(mocks):
    return mocks.write_results.call_args[0][0][0]


# ---------------------------------------------------------------------------
# _parse_amount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$1,359.37", 1359.37),
        ("*406.25", 406.25),
        ("*$1,988.27", 1988.27),
        ("  $62,760.03  ", 62760.03),
        (62760.03, 62760.03),
        (4471, 4471.0),
        (None, None),
        ("", None),
        ("n/a", None),
        ("--", None),
        ([], None),
    ],
)
def test_parse_amount(raw, expected):
    assert _parse_amount(raw) == expected


def test_parse_amount_rejects_bool():
    # bool is a subclass of int; True must not become 1.0.
    assert _parse_amount(True) is None


# ---------------------------------------------------------------------------
# _normalize_panel
# ---------------------------------------------------------------------------


def test_normalize_panel_coerces_currency_strings_to_floats():
    panel = _panel(rows=[
        {"rank": 1, "payout": "$1,359.37", "payout_marked": False, "bounty": "$281.25"},
    ])

    row = _normalize_panel(panel)["rows"][0]
    assert row["payout"] == 1359.37
    assert row["bounty"] == 281.25


def test_normalize_panel_infers_payout_marked_from_a_leading_asterisk():
    # Only fires on the string path; the prompt asks for numbers, so this is
    # belt-and-braces, not a cross-check on the model's own payout_marked.
    panel = _panel(rows=[
        {"rank": 1, "payout": "*406.25", "payout_marked": False, "bounty": None},
    ])

    row = _normalize_panel(panel)["rows"][0]
    assert row["payout_marked"] is True
    assert row["payout"] == 406.25


def test_normalize_panel_keeps_model_payout_marked_on_the_numeric_path():
    panel = _panel(rows=[
        {"rank": 1, "payout": 406.25, "payout_marked": True, "bounty": None},
    ])

    assert _normalize_panel(panel)["rows"][0]["payout_marked"] is True


def test_normalize_panel_preserves_unreadable_values_as_null():
    panel = _panel(rows=[
        {"rank": 7, "payout": None, "payout_marked": False, "bounty": None},
    ])

    row = _normalize_panel(panel)["rows"][0]
    assert row["payout"] is None
    assert row["bounty"] is None


# ---------------------------------------------------------------------------
# _derive_bounty_type / _transient_status / check_preconditions
# ---------------------------------------------------------------------------


def test_derive_bounty_type_progressive():
    assert _derive_bounty_type(_panel(has_bounty_column=True)) == "progressive"


def test_derive_bounty_type_none():
    assert _derive_bounty_type(_panel(has_bounty_column=False)) == "none"


@pytest.mark.parametrize("value", [None, "true", 1, "yes"])
def test_derive_bounty_type_rejects_non_boolean(value):
    with pytest.raises(GeminiPermanentError, match="has_bounty_column"):
        _derive_bounty_type(_panel(has_bounty_column=value))


def test_transient_status_below_cap():
    assert _transient_status(0, 3) == "failed_transient"
    assert _transient_status(1, 3) == "failed_transient"


def test_transient_status_at_cap():
    # The current failure is not yet counted, hence the +1 in the threshold.
    assert _transient_status(2, 3) == "failed_parked"
    assert _transient_status(3, 3) == "failed_parked"


def test_check_preconditions_passes_for_a_normal_broadcast():
    assert check_preconditions(_pending()) is None


@pytest.mark.parametrize("duration", [0, 1, 5])
def test_check_preconditions_skips_a_video_shorter_than_the_first_rung(duration):
    reason = check_preconditions(_pending(duration_seconds=duration))
    assert reason is not None
    assert reason.startswith("skipped:")
    assert str(FRAME_FALLBACK_LADDER[0]) in reason


# ---------------------------------------------------------------------------
# _find_pending_videos
# ---------------------------------------------------------------------------


def test_find_pending_videos_selects_only_pending_statuses():
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "a.latest_status IS NULL OR a.latest_status = 'failed_transient'" in query


def test_find_pending_videos_has_no_output_existence_guard():
    # Several outcomes are legitimately terminal with zero stage rows; a
    # NOT EXISTS filter would mark them permanently pending.
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "NOT EXISTS" not in query
    assert "tournament_results`" not in query.replace(
        "tournament_results_processing_attempts`", ""
    )


def test_find_pending_videos_counts_consecutive_failures_since_last_non_failure():
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "last_non_failure_at IS NULL OR attempted_at > last_non_failure_at" in query
    assert "status NOT LIKE 'failed%'" in query


def test_find_pending_videos_drives_off_videos_and_carries_duration():
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", client=client)

    query = client.query.call_args[0][0]
    assert "FROM `proj.ds.videos` v" in query
    assert "v.duration_seconds" in query


def test_find_pending_videos_scopes_to_supplied_ids():
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", only_video_ids=["a", "b"], client=client)

    query = client.query.call_args[0][0]
    assert "AND v.video_id IN UNNEST(@only_video_ids)" in query
    params = client.query.call_args[1]["job_config"].query_parameters
    assert params[0].values == ["a", "b"]


def test_find_pending_videos_empty_scope_list_scopes_to_nothing():
    # `is not None`, not truthiness: an empty list must bound the query to zero
    # videos rather than silently widening it to all of them.
    client = _mock_bq_client()
    _find_pending_videos("proj", "ds", only_video_ids=[], client=client)

    query = client.query.call_args[0][0]
    assert "AND v.video_id IN UNNEST(@only_video_ids)" in query


def test_find_pending_videos_maps_rows_to_dataclass():
    client = _mock_bq_client(rows=[
        _bq_row(video_id="vid1", duration_seconds=3000, consecutive_failures=2),
    ])

    result = _find_pending_videos("proj", "ds", client=client)

    assert result == [
        PendingVideo(video_id="vid1", duration_seconds=3000, consecutive_failures=2)
    ]


# ---------------------------------------------------------------------------
# process_video — the frame fallback ladder
# ---------------------------------------------------------------------------


def test_ladder_first_rung_succeeds_and_the_rest_are_never_extracted():
    with _patched([_panel()]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.frame.call_count == 1
    assert mocks.extract.call_count == 1
    assert mocks.extract.call_args[0][1] == FRAME_FALLBACK_LADDER[0]
    assert _written_row(mocks).frame_timestamp_seconds == FRAME_FALLBACK_LADDER[0]


def test_ladder_advances_past_a_non_visible_panel():
    with _patched([_not_visible(), _panel()]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert mocks.frame.call_count == 2
    attempted = [c[0][1] for c in mocks.extract.call_args_list]
    assert attempted == [FRAME_FALLBACK_LADDER[0], FRAME_FALLBACK_LADDER[1]]
    assert _written_row(mocks).frame_timestamp_seconds == FRAME_FALLBACK_LADDER[1]
    assert f"{FRAME_FALLBACK_LADDER[1]}s" in _attempt_row(mocks).status_message


def test_ladder_exhausted_is_permanent():
    with _patched([_not_visible()] * len(FRAME_FALLBACK_LADDER)) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    assert mocks.frame.call_count == len(FRAME_FALLBACK_LADDER)
    mocks.write_results.assert_not_called()
    assert "not visible" in _attempt_row(mocks).status_message


def test_ladder_skips_rungs_at_or_beyond_the_video_duration():
    # A rung past the end of the file would make ffmpeg fail on every attempt
    # and park the video for a deterministic condition.
    with _patched([_not_visible(), _panel()]) as mocks:
        outcome = _call(_pending(duration_seconds=40))

    assert outcome == "complete"
    attempted = [c[0][1] for c in mocks.extract.call_args_list]
    assert attempted == [5, 30]


# ---------------------------------------------------------------------------
# process_video — validation of the panel
# ---------------------------------------------------------------------------


def test_bounty_column_true_yields_progressive():
    rows = [
        {"rank": i, "payout": 1000.0 * i, "payout_marked": False, "bounty": 125.0 * i}
        for i in range(1, 6)
    ]
    with _patched([_panel(has_bounty_column=True, rows=rows)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert _written_row(mocks).bounty_type == "progressive"
    assert "bounty_type=progressive" in _attempt_row(mocks).status_message


def test_bounty_column_false_yields_none():
    with _patched([_panel(has_bounty_column=False)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert _written_row(mocks).bounty_type == "none"


def test_null_has_bounty_column_is_permanent():
    with _patched([_panel(has_bounty_column=None)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()
    assert "has_bounty_column" in _attempt_row(mocks).status_message


@pytest.mark.parametrize("symbol", [None, "", "   "])
def test_unreadable_currency_symbol_is_permanent(symbol):
    # A null would otherwise reach bq_param_type, which has no None branch, and
    # retry the video until it parks.
    with _patched([_panel(currency_symbol=symbol)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()
    assert "currency_symbol" in _attempt_row(mocks).status_message


def test_visible_panel_with_no_rows_is_permanent():
    # Otherwise a 'complete' row lands containing no payout data at all — the
    # one thing this phase exists to produce.
    with _patched([_panel(rows=[])]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()
    assert "ladder rank" in _attempt_row(mocks).status_message


def test_visible_panel_with_too_few_ranks_is_permanent():
    short = [
        {"rank": i, "payout": 1000.0 * i, "payout_marked": False, "bounty": None}
        for i in range(1, MIN_LADDER_RANKS)
    ]
    with _patched([_panel(rows=short)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()


def test_visible_panel_missing_rank_one_is_permanent():
    scrolled = [
        {"rank": i, "payout": 1000.0 * i, "payout_marked": False, "bounty": None}
        for i in range(2, MIN_LADDER_RANKS + 2)
    ]
    with _patched([_panel(rows=scrolled)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()
    assert "rank 1" in _attempt_row(mocks).status_message


# ---------------------------------------------------------------------------
# process_video — the written row
# ---------------------------------------------------------------------------


def test_written_row_nests_the_panel_under_a_named_key():
    with _patched([_panel()]) as mocks:
        _call(_pending())

    row = _written_row(mocks)
    assert set(row.tournament_results_state) == {"panel"}
    assert row.tournament_results_state["panel"]["rows"][0]["payout"] == 62760.03


def test_written_row_carries_normalised_amounts():
    with _patched([_panel(rows=[
        {"rank": 1, "payout": "$62,760.03", "payout_marked": True, "bounty": None},
        {"rank": 2, "payout": "*54407.04", "payout_marked": False, "bounty": None},
        {"rank": 3, "payout": 49632.96, "payout_marked": False, "bounty": None},
        {"rank": 4, "payout": 26271.31, "payout_marked": False, "bounty": None},
        {"rank": 5, "payout": None, "payout_marked": False, "bounty": None},
    ])]) as mocks:
        _call(_pending())

    rows = _written_row(mocks).tournament_results_state["panel"]["rows"]
    assert [r["payout"] for r in rows] == [62760.03, 54407.04, 49632.96, 26271.31, None]
    assert [r["payout_marked"] for r in rows] == [True, True, False, False, False]


def test_write_is_keyed_on_video_id():
    with _patched([_panel()]) as mocks:
        _call(_pending(video_id="somevid"))

    assert mocks.write_results.call_args[1]["video_id"] == "somevid"
    assert _written_row(mocks).video_id == "somevid"


def test_attempt_row_is_written_after_the_stage_row():
    # Safe only because the stage write has replace semantics: a failure of the
    # attempt write reproduces the same row on retry rather than duplicating it.
    calls = []
    with _patched([_panel()]) as mocks:
        mocks.write_results.side_effect = lambda *a, **k: calls.append("stage")
        mocks.write_attempt.side_effect = lambda *a, **k: calls.append("attempt")
        _call(_pending())

    assert calls == ["stage", "attempt"]


# ---------------------------------------------------------------------------
# process_video — frame retention
# ---------------------------------------------------------------------------


def test_upload_precedes_the_stage_write():
    # The one-directional guarantee: a live row's frame_gcs_path always
    # resolves, because the object is in GCS before the row referencing it
    # exists. The attempt row is written last.
    call_order = []
    with _patched([_panel()]) as mocks:
        mocks.upload.side_effect = lambda *a, **k: call_order.append("upload")
        mocks.write_results.side_effect = lambda *a, **k: call_order.append("write_results")
        mocks.write_attempt.side_effect = lambda *a, **k: call_order.append("write_attempt")
        outcome = _call(_pending())

    assert outcome == "complete"
    assert call_order == ["upload", "write_results", "write_attempt"]


def test_upload_failure_is_transient_and_writes_no_stage_row():
    # frame_uploader lets GCS errors propagate unwrapped, so this lands in the
    # catch-all. Because the upload precedes the write, no row is left behind
    # pointing at an object that was never stored.
    with _patched([_panel()]) as mocks:
        mocks.upload.side_effect = RuntimeError("GCS unavailable")
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    mocks.write_results.assert_not_called()
    assert "GCS unavailable" in _attempt_row(mocks).status_message


def test_upload_failure_at_the_cap_parks():
    with _patched([_panel()]) as mocks:
        mocks.upload.side_effect = RuntimeError("GCS unavailable")
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"
    mocks.write_results.assert_not_called()


def test_frame_path_is_deterministic_and_carries_no_timestamp():
    with _patched([_panel()]) as mocks:
        _call(_pending(video_id="somevid"))

    expected = f"gs://{_BUCKET}/somevid/results.jpg"
    assert mocks.upload.call_args[0][1] == expected
    assert _written_row(mocks).frame_gcs_path == expected
    # No ladder rung may appear in the path — frame_timestamp_seconds records
    # which rung won, and a timestamped path would orphan the prior object on
    # every reprocess that succeeded at a different rung.
    for rung in FRAME_FALLBACK_LADDER:
        assert str(rung) not in expected.rsplit("/", 1)[-1]


def test_a_later_rung_uploads_to_the_same_path_as_the_first():
    # The property that makes reprocessing non-orphaning: whichever rung wins,
    # the object lands in the same place and overwrites.
    with _patched([_panel()]) as first:
        _call(_pending(video_id="somevid"))
    with _patched([_not_visible(), _panel()]) as second:
        _call(_pending(video_id="somevid"))

    assert second.frame.call_count == 2
    assert first.upload.call_args[0][1] == second.upload.call_args[0][1]
    # ...even though the recorded rung differs.
    assert (
        _written_row(first).frame_timestamp_seconds
        != _written_row(second).frame_timestamp_seconds
    )


def test_written_row_carries_a_non_null_frame_gcs_path():
    with _patched([_panel()]) as mocks:
        _call(_pending())

    row = _written_row(mocks)
    assert row.frame_gcs_path
    assert row.frame_gcs_path.startswith("gs://")


def test_upload_is_from_the_local_frame_the_panel_was_read_from():
    with _patched([_not_visible(), _panel()]) as mocks:
        _call(_pending())

    # The second rung's frame, not the first — the file the winning read used.
    winning_local_path = mocks.extract.call_args_list[1][0][2]
    assert mocks.upload.call_args[0][0] == winning_local_path


@pytest.mark.parametrize(
    "panel",
    [
        _panel(currency_symbol=None),
        _panel(has_bounty_column=None),
        _panel(rows=[]),
    ],
)
def test_no_upload_when_the_panel_fails_validation(panel):
    # Validation precedes the upload, so a permanently-failing video leaves no
    # object behind at all.
    with _patched([panel]) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.upload.assert_not_called()
    mocks.write_results.assert_not_called()


def test_no_upload_when_the_ladder_is_exhausted():
    with _patched([_not_visible()] * len(FRAME_FALLBACK_LADDER)) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.upload.assert_not_called()


# ---------------------------------------------------------------------------
# process_video — failure classification
# ---------------------------------------------------------------------------


def test_gemini_permanent_error_is_permanent():
    with _patched(GeminiPermanentError("malformed JSON from Gemini")) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_permanent"
    mocks.write_results.assert_not_called()
    assert _attempt_row(mocks).status == "failed_permanent"


def test_gemini_transient_error_falls_to_the_catch_all():
    with _patched(GeminiTransientError("rate limited")) as mocks:
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert _attempt_row(mocks).status == "failed_transient"


def test_transient_failure_at_the_cap_parks():
    with _patched(GeminiTransientError("rate limited")) as mocks:
        outcome = _call(_pending(consecutive_failures=2))

    assert outcome == "failed_parked"
    assert _attempt_row(mocks).status == "failed_parked"


def test_write_failure_is_transient():
    with _patched([_panel()]) as mocks:
        mocks.write_results.side_effect = RuntimeError("BQ unavailable")
        outcome = _call(_pending())

    assert outcome == "failed_transient"
    assert "BQ unavailable" in _attempt_row(mocks).status_message


def test_status_message_is_truncated():
    with _patched(GeminiPermanentError("x" * 900)) as mocks:
        _call(_pending())

    assert len(_attempt_row(mocks).status_message) == 500


def test_process_video_never_raises():
    with _patched(RuntimeError("anything at all")):
        outcome = _call(_pending())

    assert outcome == "failed_transient"


# ---------------------------------------------------------------------------
# process_pending_videos
# ---------------------------------------------------------------------------


@contextmanager
def _patched_orchestrator(pending, process_outcomes=None, download_error=None):
    with (
        patch(
            "table_talk.payout_processing._find_pending_videos", return_value=pending
        ) as find,
        patch(
            "table_talk.payout_processing.download_video", side_effect=download_error
        ) as download,
        patch(
            "table_talk.payout_processing.process_video",
            side_effect=process_outcomes or (lambda *a, **k: "complete"),
        ) as process,
        patch(
            "table_talk.payout_processing.write_tournament_results_processing_attempt_row"
        ) as write_attempt,
    ):
        yield SimpleNamespace(
            find=find, download=download, process=process, write_attempt=write_attempt
        )


def test_pending_videos_precondition_skip_never_downloads():
    # The whole point of checking ahead of the download: the entity IS the
    # video, so there is nothing to amortise a 100-200 MB fetch over.
    with _patched_orchestrator([_pending(duration_seconds=1)]) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    mocks.download.assert_not_called()
    mocks.process.assert_not_called()
    assert stats["videos_complete_skipped"] == 1
    assert stats["videos_processed"] == 1
    assert mocks.write_attempt.call_args[0][0].status == "complete_skipped"


def test_pending_videos_happy_path_downloads_then_processes():
    with _patched_orchestrator([_pending()]) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    mocks.download.assert_called_once()
    assert mocks.download.call_args[0][0] == "gs://vb/dQw4w9WgXcQ.mp4"
    assert stats["videos_complete"] == 1
    assert stats["videos_processed"] == 1


def test_pending_videos_download_404_is_permanent():
    with _patched_orchestrator(
        [_pending()], download_error=DownloadPermanentError("Video object not found")
    ) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    mocks.process.assert_not_called()
    assert stats["videos_failed_permanent"] == 1
    row = mocks.write_attempt.call_args[0][0]
    assert row.status == "failed_permanent"
    assert row.status_message.startswith("video_download_not_found:")


def test_pending_videos_download_error_is_transient():
    with _patched_orchestrator(
        [_pending()], download_error=RuntimeError("connection reset")
    ) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    mocks.process.assert_not_called()
    assert stats["videos_failed_transient"] == 1
    assert mocks.write_attempt.call_args[0][0].status_message.startswith(
        "video_download_failed:"
    )


def test_pending_videos_download_error_at_the_cap_parks():
    with _patched_orchestrator(
        [_pending(consecutive_failures=2)], download_error=RuntimeError("connection reset")
    ) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    assert stats["videos_failed_parked"] == 1
    assert mocks.write_attempt.call_args[0][0].status == "failed_parked"


def test_pending_videos_scopes_to_video_id():
    with _patched_orchestrator([]) as mocks:
        _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT, video_id="somevid"))

    assert mocks.find.call_args[1]["only_video_ids"] == ["somevid"]


def test_pending_videos_no_video_id_scopes_to_everything():
    with _patched_orchestrator([]) as mocks:
        _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    assert mocks.find.call_args[1]["only_video_ids"] is None


def test_pending_videos_stats_shape_with_no_work():
    with _patched_orchestrator([]):
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    assert stats == {
        "videos_processed": 0,
        "videos_complete": 0,
        "videos_complete_skipped": 0,
        "videos_failed_transient": 0,
        "videos_failed_permanent": 0,
        "videos_failed_parked": 0,
    }


def test_pending_videos_continues_past_a_failing_video():
    outcomes = iter(["failed_permanent", "complete"])

    def _next(*args, **kwargs):
        return next(outcomes)

    with _patched_orchestrator(
        [_pending(video_id="a"), _pending(video_id="b")], process_outcomes=_next
    ) as mocks:
        stats = _run(process_pending_videos("proj", "ds", "vb", _BUCKET, _PROMPT))

    assert mocks.process.call_count == 2
    assert stats["videos_processed"] == 2
    assert stats["videos_failed_permanent"] == 1
    assert stats["videos_complete"] == 1


# ---------------------------------------------------------------------------
# Integration tests
#
# Heavy imports are deferred inside each test so the unit suite does not pay for
# them. Setup goes through Phase 1's production writer, never its orchestrator,
# per CLAUDE.md's cross-phase setup rule.
# ---------------------------------------------------------------------------

_INTEGRATION_PROJECT = "table-talk-497020"
_INTEGRATION_DATASET = "table_talk_dev"
_INTEGRATION_VIDEOS_BUCKET = "table-talk-497020-videos-dev"
_INTEGRATION_RESULTS_BUCKET = "table-talk-497020-tournament-results-dev"


def _seed_video(bq_client, *, duration_seconds=3000, uid_tag="payouts"):
    from table_talk.videos_writer import VideosRow, write_video_row

    uid = uuid.uuid4().hex[:10]
    video_id = f"test_{uid_tag}_{uid}"
    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Payout extraction integration test",
            duration_seconds=duration_seconds,
            gcs_path=f"gs://{_INTEGRATION_VIDEOS_BUCKET}/{video_id}.mp4",
            file_size_bytes=12345,
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )
    return video_id


def _upload_fixture_video(gcs_client, video_id, duration_seconds=1):
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

    blob = gcs_client.bucket(_INTEGRATION_VIDEOS_BUCKET).blob(f"{video_id}.mp4")
    blob.upload_from_string(video_bytes, content_type="video/mp4")
    return blob


def _write_attempt_row(bq_client, video_id, status):
    from table_talk._generated.tournament_results_processing_attempts_row import (
        TournamentResultsProcessingAttemptsRow,
    )
    from table_talk.tournament_results_processing_attempts_writer import (
        write_tournament_results_processing_attempt_row,
    )

    write_tournament_results_processing_attempt_row(
        TournamentResultsProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            video_id=video_id,
            status=status,
            status_message=f"integration seed: {status}",
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=bq_client,
    )


def _cleanup(bq_client, video_id):
    from google.cloud import bigquery

    refs = [
        (f"{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.tournament_results", "video_id"),
        (
            f"{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}."
            "tournament_results_processing_attempts",
            "video_id",
        ),
        (f"{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.videos", "video_id"),
    ]
    for table, col in refs:
        bq_client.query(
            f"DELETE FROM `{table}` WHERE {col} = @val",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("val", "STRING", video_id)]
            ),
        ).result()


@pytest.mark.integration
def test_precondition_skip_end_to_end_makes_no_gemini_call():
    from google.cloud import bigquery, storage

    bq_client = bigquery.Client(project=_INTEGRATION_PROJECT)
    gcs_client = storage.Client(project=_INTEGRATION_PROJECT)
    video_id = _seed_video(bq_client, duration_seconds=1)
    video_blob = None

    try:
        # Uploaded even though the skip should precede the download, so that a
        # regression which reinstates the download-first ordering fails on the
        # assertion below rather than on a 404.
        video_blob = _upload_fixture_video(gcs_client, video_id)

        with patch("table_talk.payout_processing.call_gemini_for_frame") as frame:
            stats = _run(
                process_pending_videos(
                    _INTEGRATION_PROJECT,
                    _INTEGRATION_DATASET,
                    _INTEGRATION_VIDEOS_BUCKET,
                    _INTEGRATION_RESULTS_BUCKET,
                    _PROMPT,
                    video_id=video_id,
                    bq_client=bq_client,
                    gcs_client=gcs_client,
                )
            )

        frame.assert_not_called()
        assert stats["videos_complete_skipped"] == 1

        rows = list(bq_client.query(
            f"SELECT * FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}."
            f"tournament_results` WHERE video_id = '{video_id}'"
        ).result())
        assert rows == []

        attempts = list(bq_client.query(
            f"SELECT status FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}."
            f"tournament_results_processing_attempts` WHERE video_id = '{video_id}'"
        ).result())
        assert [a.status for a in attempts] == ["complete_skipped"]
    finally:
        if video_blob is not None and video_blob.exists():
            video_blob.delete()
        _cleanup(bq_client, video_id)


@pytest.mark.integration
def test_frame_is_retained_in_gcs_at_the_deterministic_path():
    """process_video uploads the real frame, then writes the row pointing at it.

    Gemini and the video download are patched — this exercises the GCS write and
    the row's frame_gcs_path, not the extraction.
    """
    from google.cloud import bigquery, storage

    bq_client = bigquery.Client(project=_INTEGRATION_PROJECT)
    gcs_client = storage.Client(project=_INTEGRATION_PROJECT)
    video_id = _seed_video(bq_client)

    object_path = f"{video_id}/results.jpg"
    expected_uri = f"gs://{_INTEGRATION_RESULTS_BUCKET}/{object_path}"
    # Handle built before the try, so finally can always reach it.
    blob = gcs_client.bucket(_INTEGRATION_RESULTS_BUCKET).blob(object_path)

    try:
        with (
            patch(
                "table_talk.payout_processing.extract_frame",
                side_effect=_fake_extract_frame,
            ),
            patch(
                "table_talk.payout_processing.call_gemini_for_frame",
                return_value=_panel(),
            ),
        ):
            outcome = _run(
                process_video(
                    _pending(video_id=video_id),
                    "/tmp/does-not-exist.mp4",
                    _INTEGRATION_PROJECT,
                    _INTEGRATION_DATASET,
                    _INTEGRATION_RESULTS_BUCKET,
                    _PROMPT,
                )
            )

        assert outcome == "complete"
        assert blob.exists()

        rows = list(bq_client.query(
            f"SELECT frame_gcs_path FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}."
            f"tournament_results` WHERE video_id = '{video_id}'"
        ).result())
        assert len(rows) == 1
        assert rows[0].frame_gcs_path == expected_uri

        # Reprocess: the stable path overwrites rather than adding a second
        # object, which is what keeps reprocessing non-orphaning.
        with (
            patch(
                "table_talk.payout_processing.extract_frame",
                side_effect=_fake_extract_frame,
            ),
            patch(
                "table_talk.payout_processing.call_gemini_for_frame",
                return_value=_panel(),
            ),
        ):
            assert _run(
                process_video(
                    _pending(video_id=video_id),
                    "/tmp/does-not-exist.mp4",
                    _INTEGRATION_PROJECT,
                    _INTEGRATION_DATASET,
                    _INTEGRATION_RESULTS_BUCKET,
                    _PROMPT,
                )
            ) == "complete"

        objects = list(
            gcs_client.bucket(_INTEGRATION_RESULTS_BUCKET).list_blobs(prefix=f"{video_id}/")
        )
        assert [o.name for o in objects] == [object_path]
    finally:
        if blob.exists():
            blob.delete()
        _cleanup(bq_client, video_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "seeded_statuses,expect_selected,expect_failures",
    [
        ([], True, 0),
        (["failed_transient"], True, 1),
        (["failed_transient", "failed_transient", "failed_transient"], True, 3),
        (["failed_transient", "complete", "failed_transient"], True, 1),
        (["failed_transient", "complete"], False, None),
        (["complete"], False, None),
        (["complete_skipped"], False, None),
        (["failed_permanent"], False, None),
        (["failed_transient", "failed_parked"], False, None),
    ],
)
def test_pending_query_selection_semantics(
    seeded_statuses, expect_selected, expect_failures
):
    from google.cloud import bigquery

    bq_client = bigquery.Client(project=_INTEGRATION_PROJECT)
    video_id = _seed_video(bq_client)

    try:
        for status in seeded_statuses:
            _write_attempt_row(bq_client, video_id, status)

        pending = _find_pending_videos(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_video_ids=[video_id],
            client=bq_client,
        )

        if not expect_selected:
            assert pending == []
        else:
            assert len(pending) == 1
            assert pending[0].video_id == video_id
            assert pending[0].duration_seconds == 3000
            assert pending[0].consecutive_failures == expect_failures
    finally:
        _cleanup(bq_client, video_id)


def test_rank_one_is_recognised_when_returned_as_a_string():
    # The spike saw this model return numbers as strings on some frames; a
    # string rank must not fail an otherwise good panel.
    rows = [
        {"rank": str(i), "payout": 1000.0 * i, "payout_marked": False, "bounty": None}
        for i in range(1, MIN_LADDER_RANKS + 1)
    ]
    with _patched([_panel(rows=rows)]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    mocks.write_results.assert_called_once()


def test_currency_symbol_is_stored_trimmed():
    with _patched([_panel(currency_symbol="  $ ")]) as mocks:
        outcome = _call(_pending())

    assert outcome == "complete"
    assert _written_row(mocks).currency_symbol == "$"
