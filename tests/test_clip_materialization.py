import uuid
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery

from table_talk._generated.clip_materialization_attempts_row import (
    ClipMaterializationAttemptsRow,
)
from table_talk.clip_manifest_writer import ClipManifestRow
from table_talk.clip_materialization import (
    MaterializeError,
    PendingVideo,
    _find_pending_videos,
    _transient_status,
    materialize_clips,
    materialize_clips_for_pending_videos,
)
from table_talk.clip_materialization_attempts_writer import (
    write_clip_materialization_attempt_row,
)
from table_talk.videos_writer import VideosRow, write_video_row

PROJECT = "test-project"
DATASET = "test_dataset"
VIDEO_ID = "dQw4w9WgXcQ"

WRITE_MANIFEST = "table_talk.clip_materialization.write_clip_manifest_rows"
WRITE_ATTEMPT = "table_talk.clip_materialization.write_clip_materialization_attempt_row"


def _state_row(
    video_id=VIDEO_ID,
    duration_seconds=480,
    has_payouts=True,
    payout_latest_status=None,
    consecutive_failures=0,
):
    """One row of the video-state query both entry points read."""
    mock_row = MagicMock()
    mock_row.video_id = video_id
    mock_row.duration_seconds = duration_seconds
    mock_row.has_payouts = has_payouts
    mock_row.payout_latest_status = payout_latest_status
    mock_row.consecutive_failures = consecutive_failures
    return mock_row


def _mock_bq(*query_results):
    """A BQ client whose successive query() calls resolve to the given row lists.

    Both entry points call .result() on the job, so each result is wrapped.
    """
    jobs = []
    for rows in query_results:
        job = MagicMock()
        job.result.return_value = rows
        jobs.append(job)
    mock = MagicMock()
    mock.query.side_effect = jobs
    return mock


def _statuses(mock_attempt):
    return [c[0][0].status for c in mock_attempt.call_args_list]


# --- materialize_clips unit tests ---


def test_240s_aligned_video():
    bq = _mock_bq([_state_row(duration_seconds=480)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_called_once()
    rows = mock_write.call_args[0][0]
    assert len(rows) == 2
    assert rows[0] == ClipManifestRow(clip_id=f"{VIDEO_ID}_001", video_id=VIDEO_ID, clip_start_time=0, clip_end_time=240)
    assert rows[1] == ClipManifestRow(clip_id=f"{VIDEO_ID}_002", video_id=VIDEO_ID, clip_start_time=240, clip_end_time=480)


def test_non_aligned_video():
    bq = _mock_bq([_state_row(duration_seconds=300)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    rows = mock_write.call_args[0][0]
    assert len(rows) == 2
    assert rows[0].clip_start_time == 0 and rows[0].clip_end_time == 240
    assert rows[1].clip_start_time == 240 and rows[1].clip_end_time == 300


def test_video_shorter_than_240s():
    bq = _mock_bq([_state_row(duration_seconds=60)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    rows = mock_write.call_args[0][0]
    assert len(rows) == 1
    assert rows[0].clip_start_time == 0 and rows[0].clip_end_time == 60


def test_long_video_ordinals():
    # 10 clips: 9 full 240s windows + 1 remainder
    bq = _mock_bq([_state_row(duration_seconds=2161)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    rows = mock_write.call_args[0][0]
    assert len(rows) == 10
    assert rows[0].clip_id == f"{VIDEO_ID}_001"
    assert rows[8].clip_id == f"{VIDEO_ID}_009"
    assert rows[9].clip_id == f"{VIDEO_ID}_010"


def test_clip_id_format():
    bq = _mock_bq([_state_row(duration_seconds=300)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    rows = mock_write.call_args[0][0]
    assert rows[0].clip_id == f"{VIDEO_ID}_001"
    assert rows[1].clip_id == f"{VIDEO_ID}_002"


def test_writer_receives_the_replace_key():
    """The natural key is an explicit writer parameter, never derived from the
    rows — an empty row list must still delete the video's existing rows."""
    bq = _mock_bq([_state_row(duration_seconds=300)])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_write.call_args[1]["video_id"] == VIDEO_ID


@pytest.mark.parametrize("duration", [0, -1])
def test_invalid_duration_raises(duration):
    bq = _mock_bq([_state_row(duration_seconds=duration)])

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST) as mock_write:
        with pytest.raises(MaterializeError, match="invalid duration_seconds"):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()
    assert _statuses(mock_attempt) == ["failed_permanent"]


def test_video_not_in_videos_table_raises():
    bq = _mock_bq([])  # empty result — video not found

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST) as mock_write:
        with pytest.raises(MaterializeError, match="not found"):
            materialize_clips("unknown_id", project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()
    # Recorded before raising: a --video-id run always leaves a trace.
    assert _statuses(mock_attempt) == ["failed_permanent"]


def test_single_video_query_is_not_filtered_by_status():
    """--video-id must work whatever the latest status is: deliberate
    reprocessing after a prompt fix is the point of the entry point."""
    bq = _mock_bq([_state_row()])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST):
        materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    query_str = bq.query.call_args[0][0]
    assert "v.video_id = @video_id" in query_str
    assert "latest_status IS NULL" not in query_str


def test_single_video_transient_failure_writes_attempt_then_raises():
    bq = _mock_bq([_state_row()])

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST, side_effect=RuntimeError("bq down")):
        with pytest.raises(MaterializeError, match="bq down"):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    assert _statuses(mock_attempt) == ["failed_transient"]


def test_single_video_parks_at_the_cap():
    """--video-id honours the cap too. consecutive_failures is read in the
    opening query, not in the failure handler, which runs precisely when
    BigQuery may be unreachable."""
    bq = _mock_bq([_state_row(consecutive_failures=2)])

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST, side_effect=RuntimeError("bq down")):
        with pytest.raises(MaterializeError):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq, max_attempts=3)

    assert _statuses(mock_attempt) == ["failed_parked"]


# --- retry cap ---


@pytest.mark.parametrize(
    "consecutive_failures,expected",
    [
        (0, "failed_transient"),
        (1, "failed_transient"),
        (2, "failed_parked"),
        (3, "failed_parked"),
    ],
)
def test_transient_status_boundary(consecutive_failures, expected):
    # The current failure is not yet counted, hence the +1 inside.
    assert _transient_status(consecutive_failures, 3) == expected


# --- materialize_clips_for_pending_videos unit tests ---


def _pending_bq(*rows):
    bq = MagicMock()
    bq.query.return_value.result.return_value = list(rows)
    return bq


def test_finds_pending_videos():
    bq = _pending_bq(_state_row(video_id="vid_aaa"), _state_row(video_id="vid_bbb"))

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        stats = materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_write.call_count == 2
    called_ids = {c[1]["video_id"] for c in mock_write.call_args_list}
    assert called_ids == {"vid_aaa", "vid_bbb"}
    assert stats["videos_processed"] == 2
    assert stats["videos_complete"] == 2


def test_no_pending_videos():
    bq = _pending_bq()

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST) as mock_write:
        stats = materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()
    mock_attempt.assert_not_called()
    assert stats["videos_processed"] == 0


def test_continues_on_per_video_failure():
    bq = _pending_bq(
        _state_row(video_id="vid_aaa"),
        _state_row(video_id="vid_bbb"),
        _state_row(video_id="vid_ccc"),
    )

    def fake_write(rows, **kwargs):
        if kwargs["video_id"] == "vid_bbb":
            raise RuntimeError("simulated failure")

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST, side_effect=fake_write) as mock_write:
        stats = materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_write.call_count == 3
    assert stats["videos_processed"] == 3
    assert stats["videos_complete"] == 2
    assert stats["videos_failed_transient"] == 1


def test_stats_keys_are_preseeded_and_sum_to_processed():
    """Every status has a bucket up front. A dict populated only as outcomes
    occur invites the `if key in stats` guard that silently drops a status."""
    bq = _pending_bq(
        _state_row(video_id="vid_ok"),
        _state_row(video_id="vid_blocked", has_payouts=False),
        _state_row(video_id="vid_bad", duration_seconds=0),
    )

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST):
        stats = materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert set(stats) == {
        "videos_processed",
        "videos_complete",
        "videos_blocked_upstream",
        "videos_failed_transient",
        "videos_failed_permanent",
        "videos_failed_parked",
    }
    outcomes = sum(v for k, v in stats.items() if k != "videos_processed")
    assert outcomes == stats["videos_processed"] == 3


def test_only_video_ids_scopes_query():
    bq = _pending_bq(_state_row(video_id="vid_aaa"), _state_row(video_id="vid_bbb"))

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST) as mock_write:
        materialize_clips_for_pending_videos(
            project=PROJECT,
            dataset=DATASET,
            bq_client=bq,
            only_video_ids=["vid_aaa", "vid_bbb"],
        )

    call_args = bq.query.call_args
    assert "IN UNNEST(@only_video_ids)" in call_args[0][0]
    param_names = [p.name for p in call_args[1]["job_config"].query_parameters]
    assert "only_video_ids" in param_names
    assert mock_write.call_count == 2


def test_empty_only_video_ids_scopes_to_nothing():
    """`is not None`, not truthiness: an empty scope list must select nothing,
    not silently widen to the whole corpus."""
    bq = _pending_bq()

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST):
        materialize_clips_for_pending_videos(
            project=PROJECT, dataset=DATASET, bq_client=bq, only_video_ids=[]
        )

    call_args = bq.query.call_args
    assert "IN UNNEST(@only_video_ids)" in call_args[0][0]
    params = {p.name: p for p in call_args[1]["job_config"].query_parameters}
    assert params["only_video_ids"].values == []


def test_no_only_video_ids_scans_all_pending():
    bq = _pending_bq(_state_row(video_id="vid_aaa"))

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST):
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    call_args = bq.query.call_args
    assert "IN UNNEST" not in call_args[0][0]
    assert call_args[1].get("job_config") is None


# --- pending query shape ---


def test_pending_query_selects_on_attempt_status_not_output_rows():
    bq = _pending_bq()

    _find_pending_videos(PROJECT, DATASET, client=bq)

    query_str = bq.query.call_args[0][0]
    assert f"`{PROJECT}.{DATASET}.clip_materialization_attempts`" in query_str
    assert (
        "(a.latest_status IS NULL OR a.latest_status IN "
        "('failed_transient', 'blocked_upstream'))" in query_str
    )
    # Pending is a property of the attempts table alone. A guard against
    # existing output rows would block the deliberate reprocessing that
    # replace-semantics writes exist to make safe.
    assert f"`{PROJECT}.{DATASET}.clip_manifest`" not in query_str


def test_pending_query_joins_tournament_results():
    bq = _pending_bq()

    _find_pending_videos(PROJECT, DATASET, client=bq)

    query_str = bq.query.call_args[0][0]
    assert f"`{PROJECT}.{DATASET}.tournament_results`" in query_str
    assert f"`{PROJECT}.{DATASET}.tournament_results_processing_attempts`" in query_str
    # LEFT, not INNER: a payout-less video must be visible-and-blocked rather
    # than dropped from the result set, because a video that never appears
    # cannot be reported.
    assert "LEFT JOIN `test-project.test_dataset.tournament_results` tr" in query_str


def test_consecutive_failure_counter_excludes_blocked_upstream():
    """The counter keys on the 'failed%' prefix, so 'blocked_upstream' resets it
    rather than advancing it toward the cap for a condition upstream of the
    video. The property falls out of the naming."""
    bq = _pending_bq()

    _find_pending_videos(PROJECT, DATASET, client=bq)

    query_str = bq.query.call_args[0][0]
    assert "MAX(IF(status NOT LIKE 'failed%', attempted_at, NULL))" in query_str
    assert "COUNTIF(" in query_str
    assert "status = 'failed_transient'" in query_str


def test_pending_query_returns_dataclasses():
    bq = _pending_bq(_state_row(video_id="vid_aaa", consecutive_failures=2))

    videos = _find_pending_videos(PROJECT, DATASET, client=bq)

    assert videos == [
        PendingVideo(
            video_id="vid_aaa",
            duration_seconds=480,
            has_payouts=True,
            payout_latest_status=None,
            consecutive_failures=2,
        )
    ]


# --- every outcome writes exactly one attempt row ---


@pytest.mark.parametrize(
    "status,row_kwargs,write_side_effect",
    [
        ("complete", {}, None),
        ("blocked_upstream", {"has_payouts": False}, None),
        ("failed_permanent", {"duration_seconds": 0}, None),
        ("failed_transient", {"consecutive_failures": 0}, RuntimeError("bq down")),
        ("failed_parked", {"consecutive_failures": 2}, RuntimeError("bq down")),
    ],
)
def test_every_outcome_writes_exactly_one_attempt_row(status, row_kwargs, write_side_effect):
    """Pending-ness is derived from this table, so an outcome that records
    nothing leaves the retry cap unable to advance."""
    bq = _pending_bq(_state_row(video_id="vid_x", **row_kwargs))

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST, side_effect=write_side_effect):
        stats = materialize_clips_for_pending_videos(
            project=PROJECT, dataset=DATASET, bq_client=bq, max_attempts=3
        )

    assert mock_attempt.call_count == 1
    written = mock_attempt.call_args[0][0]
    assert written.status == status
    assert written.video_id == "vid_x"
    assert stats[f"videos_{status}"] == 1


def test_complete_writes_a_null_status_message():
    bq = _pending_bq(_state_row(video_id="vid_x"))

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST):
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert mock_attempt.call_args[0][0].status_message is None


# --- the payout gate ---


def test_pending_video_without_payouts_is_blocked_and_named(capsys):
    bq = _pending_bq(
        _state_row(video_id="vid_ok"),
        _state_row(video_id="vid_nopayout", has_payouts=False),
    )

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST) as mock_write:
        stats = materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert [c[1]["video_id"] for c in mock_write.call_args_list] == ["vid_ok"]
    assert sorted(_statuses(mock_attempt)) == ["blocked_upstream", "complete"]
    assert stats["videos_blocked_upstream"] == 1
    assert stats["videos_complete"] == 1

    out = capsys.readouterr().out
    assert "Blocked vid_nopayout" in out
    assert "tt extract-payouts --video-id vid_nopayout" in out
    # A block is a normal outcome of a correctly ordered pipeline. Reporting it
    # as a failure would train operators to ignore it.
    assert "Failed to materialize" not in out


@pytest.mark.parametrize(
    "payout_latest_status,expected",
    [
        (None, "no extraction attempt"),
        ("failed_transient", "failed transiently"),
        ("failed_permanent", "terminated 'failed_permanent'"),
        ("failed_parked", "terminated 'failed_parked'"),
        ("complete_skipped", "terminated 'complete_skipped'"),
        ("complete", "anomalous"),
    ],
)
def test_block_reason_distinguishes_never_attempted_from_terminal(
    payout_latest_status, expected, capsys
):
    """The operator's next move differs by cause, so the bare absence of a
    tournament_results row is not a sufficient message."""
    bq = _pending_bq(
        _state_row(video_id="vid_x", has_payouts=False, payout_latest_status=payout_latest_status)
    )

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST):
        materialize_clips_for_pending_videos(project=PROJECT, dataset=DATASET, bq_client=bq)

    assert expected in capsys.readouterr().out
    # The same reason is durably recorded, not only printed.
    assert expected in mock_attempt.call_args[0][0].status_message


def test_single_video_without_payouts_writes_attempt_then_raises():
    bq = _mock_bq([_state_row(has_payouts=False)])

    with patch(WRITE_ATTEMPT) as mock_attempt, patch(WRITE_MANIFEST) as mock_write:
        with pytest.raises(MaterializeError, match="tt extract-payouts --video-id"):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)

    mock_write.assert_not_called()
    assert _statuses(mock_attempt) == ["blocked_upstream"]


def test_single_video_payout_parked_names_the_terminal_status():
    bq = _mock_bq([_state_row(has_payouts=False, payout_latest_status="failed_parked")])

    with patch(WRITE_ATTEMPT), patch(WRITE_MANIFEST):
        with pytest.raises(MaterializeError, match="failed_parked"):
            materialize_clips(VIDEO_ID, project=PROJECT, dataset=DATASET, bq_client=bq)


# --- integration tests ---


def _write_payout_row(video_id, *, project, dataset, client, bounty_type="none"):
    """Seed the tournament_results row the materialization gate requires.

    Uses payout extraction's production writer as a setup utility, per
    CLAUDE.md's cross-phase convention — never its orchestrator, which would
    download the video and call Gemini.
    """
    from table_talk._generated.tournament_results_row import TournamentResultsRow
    from table_talk.tournament_results_writer import write_tournament_results

    write_tournament_results(
        [
            TournamentResultsRow(
                video_id=video_id,
                bounty_type=bounty_type,
                currency_symbol="$",
                frame_timestamp_seconds=5,
                frame_gcs_path=f"gs://table-talk-497020-tournament-results-dev/{video_id}/results.jpg",
                tournament_results_state={"panel": {"rows": []}},
            )
        ],
        video_id=video_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )


def _seed_video(video_id, duration, *, project, dataset, client):
    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Integration Test Video",
            duration_seconds=duration,
            gcs_path=f"gs://table-talk-497020-videos-dev/{video_id}.mp4",
            file_size_bytes=12345,
        ),
        project=project,
        dataset=dataset,
        client=client,
    )


def _seed_attempt(video_id, status, *, project, dataset, client):
    write_clip_materialization_attempt_row(
        ClipMaterializationAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            video_id=video_id,
            status=status,
            status_message=f"integration seed: {status}",
        ),
        project=project,
        dataset=dataset,
        client=client,
    )


def _cleanup(client, project, dataset, video_ids):
    """Reverse dependency order: everything else references videos."""
    tables = (
        f"{project}.{dataset}.clip_manifest",
        f"{project}.{dataset}.clip_materialization_attempts",
        f"{project}.{dataset}.tournament_results",
        f"{project}.{dataset}.videos",
    )
    for table in tables:
        for vid in video_ids:
            client.query(
                f"DELETE FROM `{table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", vid)]
                ),
            ).result()


@pytest.mark.integration
def test_materialize_clips_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id = f"test_{uuid.uuid4().hex[:8]}"

    client = bigquery.Client(project=project)
    manifest_table = f"{project}.{dataset}.clip_manifest"
    attempts_table = f"{project}.{dataset}.clip_materialization_attempts"

    def _clips():
        return list(
            client.query(
                f"SELECT * FROM `{manifest_table}` WHERE video_id = @video_id ORDER BY clip_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
                ),
            ).result()
        )

    def _attempt_statuses():
        return [
            r.status
            for r in client.query(
                f"SELECT status FROM `{attempts_table}` WHERE video_id = @video_id "
                "ORDER BY attempted_at",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
                ),
            ).result()
        ]

    try:
        _seed_video(video_id, 300, project=project, dataset=dataset, client=client)
        _write_payout_row(video_id, project=project, dataset=dataset, client=client)

        materialize_clips(video_id, project=project, dataset=dataset, bq_client=client)

        rows = _clips()
        assert len(rows) == 2
        assert rows[0].clip_id == f"{video_id}_001"
        assert rows[0].clip_start_time == 0
        assert rows[0].clip_end_time == 240
        assert rows[1].clip_id == f"{video_id}_002"
        assert rows[1].clip_start_time == 240
        assert rows[1].clip_end_time == 300
        assert rows[0].materialized_at is not None
        assert _attempt_statuses() == ["complete"]

        # Re-running replaces rather than appending, and records a second attempt.
        materialize_clips(video_id, project=project, dataset=dataset, bq_client=client)
        assert len(_clips()) == 2
        assert _attempt_statuses() == ["complete", "complete"]
    finally:
        _cleanup(client, project, dataset, [video_id])


@pytest.mark.integration
def test_pending_selection_by_latest_status():
    """No attempts and blocked_upstream are selected; complete and failed_parked
    are not. uuid-scoped, because appending to an append-only audit log can
    never be undone and must not touch a corpus video."""
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    ids = {
        "never": f"test_{uuid.uuid4().hex[:8]}",
        "blocked": f"test_{uuid.uuid4().hex[:8]}",
        "complete": f"test_{uuid.uuid4().hex[:8]}",
        "parked": f"test_{uuid.uuid4().hex[:8]}",
    }
    client = bigquery.Client(project=project)

    try:
        for vid in ids.values():
            _seed_video(vid, 300, project=project, dataset=dataset, client=client)
        _seed_attempt(ids["blocked"], "blocked_upstream", project=project, dataset=dataset, client=client)
        _seed_attempt(ids["complete"], "complete", project=project, dataset=dataset, client=client)
        _seed_attempt(ids["parked"], "failed_parked", project=project, dataset=dataset, client=client)

        selected = {
            v.video_id
            for v in _find_pending_videos(project, dataset, list(ids.values()), client=client)
        }
        assert selected == {ids["never"], ids["blocked"]}
    finally:
        _cleanup(client, project, dataset, list(ids.values()))


@pytest.mark.integration
def test_blocked_upstream_does_not_advance_the_retry_cap():
    """'blocked_upstream' is retryable but is not a 'failed%' status, so it
    resets the counter — a video waiting on a slow payout extraction must not
    park for a condition that is not its fault."""
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id = f"test_{uuid.uuid4().hex[:8]}"
    client = bigquery.Client(project=project)

    def _failures():
        return _find_pending_videos(project, dataset, [video_id], client=client)[0].consecutive_failures

    try:
        _seed_video(video_id, 300, project=project, dataset=dataset, client=client)

        for status in ("failed_transient", "failed_transient"):
            _seed_attempt(video_id, status, project=project, dataset=dataset, client=client)
        assert _failures() == 2

        _seed_attempt(video_id, "blocked_upstream", project=project, dataset=dataset, client=client)
        assert _failures() == 0

        _seed_attempt(video_id, "failed_transient", project=project, dataset=dataset, client=client)
        assert _failures() == 1
    finally:
        _cleanup(client, project, dataset, [video_id])


@pytest.mark.integration
def test_reprocessing_replaces_rather_than_doubling():
    """The property that removing the existing-clips early return depends on,
    and the rehearsal for what `tt mark-pending` will do: append a retryable
    attempt row, re-run, and the clip rows are replaced rather than doubled.

    Proven on a uuid-scoped fixture rather than a corpus video, because the
    seeded attempt row can never be removed from an append-only audit log.
    """
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id = f"test_{uuid.uuid4().hex[:8]}"
    client = bigquery.Client(project=project)
    manifest_table = f"{project}.{dataset}.clip_manifest"

    def _clip_count():
        return list(
            client.query(
                f"SELECT COUNT(1) AS n FROM `{manifest_table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
                ),
            ).result()
        )[0].n

    try:
        _seed_video(video_id, 480, project=project, dataset=dataset, client=client)
        _write_payout_row(video_id, project=project, dataset=dataset, client=client)

        stats = materialize_clips_for_pending_videos(
            project=project, dataset=dataset, bq_client=client, only_video_ids=[video_id]
        )
        assert stats["videos_complete"] == 1
        assert _clip_count() == 2

        # Now 'complete', so no longer selected.
        stats = materialize_clips_for_pending_videos(
            project=project, dataset=dataset, bq_client=client, only_video_ids=[video_id]
        )
        assert stats["videos_processed"] == 0
        assert _clip_count() == 2

        # Marking it pending re-selects it, and the re-run replaces.
        _seed_attempt(video_id, "failed_transient", project=project, dataset=dataset, client=client)
        stats = materialize_clips_for_pending_videos(
            project=project, dataset=dataset, bq_client=client, only_video_ids=[video_id]
        )
        assert stats["videos_complete"] == 1
        assert _clip_count() == 2, "replace, not append — 2 windows, not 4"
    finally:
        _cleanup(client, project, dataset, [video_id])


@pytest.mark.integration
def test_materialize_clips_for_pending_videos_integration(capsys):
    """The gate's negative case: video B has no tournament_results row on the
    first run, so it is named and recorded as blocked_upstream rather than
    materialized; adding the row makes the next run materialize it.

    Neither corpus video exercises this — both already have payout rows and are
    already materialized — so it has to be constructed with uuid-scoped ids.
    """
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    video_id_a = f"test_{uuid.uuid4().hex[:8]}"
    video_id_b = f"test_{uuid.uuid4().hex[:8]}"

    client = bigquery.Client(project=project)
    manifest_table = f"{project}.{dataset}.clip_manifest"

    def _clip_count(vid):
        return list(
            client.query(
                f"SELECT COUNT(1) AS n FROM `{manifest_table}` WHERE video_id = @video_id",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", vid)]
                ),
            ).result()
        )[0].n

    try:
        _seed_video(video_id_a, 120, project=project, dataset=dataset, client=client)
        _seed_video(video_id_b, 480, project=project, dataset=dataset, client=client)
        # A is admissible; B has no payout row yet.
        _write_payout_row(video_id_a, project=project, dataset=dataset, client=client)

        stats = materialize_clips_for_pending_videos(
            project=project,
            dataset=dataset,
            bq_client=client,
            only_video_ids=[video_id_a, video_id_b],
        )

        assert _clip_count(video_id_a) == 1
        assert _clip_count(video_id_b) == 0, "B has no payout row and must not be materialized"
        assert stats["videos_complete"] == 1
        assert stats["videos_blocked_upstream"] == 1

        out = capsys.readouterr().out
        assert f"Blocked {video_id_b}" in out
        assert f"tt extract-payouts --video-id {video_id_b}" in out

        # Supplying the payout row admits B on the next run. A is now 'complete'
        # and is not re-selected.
        _write_payout_row(
            video_id_b, project=project, dataset=dataset, client=client, bounty_type="progressive"
        )
        stats = materialize_clips_for_pending_videos(
            project=project,
            dataset=dataset,
            bq_client=client,
            only_video_ids=[video_id_a, video_id_b],
        )

        assert _clip_count(video_id_b) == 2
        assert _clip_count(video_id_a) == 1, "A must not be re-materialized"
        assert stats["videos_processed"] == 1
    finally:
        _cleanup(client, project, dataset, [video_id_a, video_id_b])
