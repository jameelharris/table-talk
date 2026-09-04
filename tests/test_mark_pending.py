import re
import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery

from table_talk import mark_pending as mark_pending_module
from table_talk._generated.clip_manifest_row import ClipManifestRow
from table_talk._generated.clip_processing_attempts_row import ClipProcessingAttemptsRow
from table_talk._generated.hand_actions_row import HandActionsRow
from table_talk._generated.hand_setup_processing_attempts_row import (
    HandSetupProcessingAttemptsRow,
)
from table_talk._generated.hand_setups_row import HandSetupsRow
from table_talk._generated.hand_start_processing_attempts_row import (
    HandStartProcessingAttemptsRow,
)
from table_talk._generated.hand_starts_row import HandStartsRow
from table_talk._generated.tournament_results_row import TournamentResultsRow
from table_talk.clip_manifest_writer import write_clip_manifest_rows
from table_talk.clip_processing_attempts_writer import write_clip_processing_attempt_row
from table_talk.hand_actions_writer import write_hand_actions
from table_talk.hand_setup_processing_attempts_writer import (
    write_hand_setup_processing_attempt_row,
)
from table_talk.hand_setups_writer import write_hand_setups
from table_talk.hand_start_processing_attempts_writer import (
    write_hand_start_processing_attempt_row,
)
from table_talk.hand_starts_writer import write_hand_starts
from table_talk.mark_pending import (
    DETAIL_THRESHOLD,
    DOWNSTREAM,
    STAGES,
    MarkPendingError,
    entity_sql,
    format_plan,
    mark_pending,
    resolve,
)
from table_talk.tournament_results_writer import write_tournament_results
from table_talk.videos_writer import VideosRow, write_video_row

PROJECT = "test-project"
DATASET = "test_dataset"
VIDEO_ID = "vid001"

_TABLE_RE = re.compile(r"`[^`]*\.([a-z_]+)`")


def _table_of(sql):
    return _TABLE_RE.findall(sql)[-1]


def _row(**kwargs):
    mock = MagicMock()
    for key, value in kwargs.items():
        setattr(mock, key, value)
    return mock


def _handler(*, entities=(), delete_counts=None, mark_ids=None):
    """Dispatch on SQL content, not call order.

    Keyed on substrings so a test does not break when an unrelated query is added
    to the resolver.
    """
    delete_counts = delete_counts or {}
    mark_ids = mark_ids or {}

    def handle(sql):
        if "output_counts AS" in sql:
            return [
                _row(entity_id=e[0], latest_status=e[1], row_count=e[2]) for e in entities
            ]
        if "SELECT COUNT(*) AS n" in sql:
            return [_row(n=delete_counts.get(_table_of(sql), 0))]
        if sql.lstrip().startswith("DELETE FROM"):
            return []
        return [_row(entity_id=i) for i in mark_ids.get(_table_of(sql), [])]

    return handle


def _mock_bq(handler, log=None):
    def query(sql, job_config=None):
        if log is not None:
            log.append(("query", sql))
        job = MagicMock()
        job.result.return_value = handler(sql)
        return job

    mock = MagicMock()
    mock.query.side_effect = query
    return mock


def _patch_writers(log=None):
    """Swap every batched writer in the registry for a mock.

    Patching the registry rather than the module attributes: `_MARK_WRITERS`
    binds the real functions at import time, so patching the names would not
    reach them.
    """
    mocks = {}
    replacement = {}
    for table, (row_class, _real) in mark_pending_module._MARK_WRITERS.items():
        mock = MagicMock()
        if log is not None:
            mock.side_effect = (
                lambda rows, *, project, dataset, client, _t=table: log.append(
                    ("mark", _t, len(rows))
                )
            )
        mocks[table] = mock
        replacement[table] = (row_class, mock)
    patcher = patch.object(mark_pending_module, "_MARK_WRITERS", replacement)
    return [patcher], mocks


def _run(stage, *, entities=(), delete_counts=None, mark_ids=None, log=None, **kwargs):
    """mark_pending against a mocked client, with every writer patched."""
    client = _mock_bq(
        _handler(entities=entities, delete_counts=delete_counts, mark_ids=mark_ids),
        log=log,
    )
    patchers, mocks = _patch_writers(log=log)
    for p in patchers:
        p.start()
    try:
        plan = mark_pending(
            stage=stage,
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            client=client,
            **kwargs,
        )
    finally:
        for p in patchers:
            p.stop()
    return plan, client, mocks


# --- the stage graph ---


def test_tournament_results_cascades_below_hand_setups():
    assert DOWNSTREAM["tournament_results"] == ("hand_setups", "hand_starts", "hand_actions")


def test_tournament_results_does_not_touch_clip_manifest():
    """The regression guard for the pointless-link mistake.

    Materialization is arithmetic on duration_seconds, so re-reading the payout
    panel cannot change a clip window. Deriving the cascade positionally from
    PHASES would reintroduce the link, which is not merely wasteful: deleting
    clip_manifest destroys clip ids and briefly orphans hand_setups.
    """
    assert "clip_manifest" not in DOWNSTREAM["tournament_results"]

    plan, client, mocks = _run(
        "tournament_results",
        entities=[(VIDEO_ID, "complete", 1)],
        mark_ids={"clip_manifest": ["c1"], "hand_setups": [], "hand_starts": []},
    )
    assert "clip_manifest" not in [t for t, _ in plan.deletes]
    assert "clip_materialization_attempts" not in [t for t, _ in plan.marks]
    mocks["clip_materialization_attempts"].assert_not_called()
    for call in client.query.call_args_list:
        sql = call.args[0].lstrip()
        assert not sql.startswith(f"DELETE FROM `{PROJECT}.{DATASET}.clip_manifest`")


def test_clip_manifest_and_tournament_results_share_a_cascade():
    assert DOWNSTREAM["clip_manifest"] == DOWNSTREAM["tournament_results"]


def test_hand_actions_is_the_leaf():
    assert DOWNSTREAM["hand_actions"] == ()


def test_videos_is_not_a_stage():
    assert "videos" not in STAGES
    with pytest.raises(MarkPendingError):
        resolve(
            stage="videos",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            client=_mock_bq(_handler()),
        )


# --- entity resolution ---


_ENTITY_TABLE = {
    "tournament_results": ("videos", "video_id"),
    "clip_manifest": ("videos", "video_id"),
    "hand_setups": ("clip_manifest", "clip_id"),
    "hand_starts": ("hand_setups", "hand_setup_id"),
    "hand_actions": ("hand_starts", "hand_start_id"),
}


@pytest.mark.parametrize("stage", STAGES)
def test_each_stage_resolves_against_the_right_entity_table(stage):
    """The off-by-one: attempts tables are named for the entity consumed."""
    input_table, key_column = _ENTITY_TABLE[stage]
    plan, client, _ = _run(stage, entities=[("e1", "complete", 1)])
    assert plan.entity_table == input_table

    entity_query = [
        c.args[0] for c in client.query.call_args_list if "output_counts AS" in c.args[0]
    ][0]
    assert f".{input_table}` i" in entity_query
    assert f"i.{key_column} AS entity_id" in entity_query


def test_an_id_from_another_video_is_rejected_before_anything_is_written():
    # The entity query is scoped to @video_id, so another video's id simply does
    # not come back.
    client = _mock_bq(_handler(entities=[]))
    patchers, mocks = _patch_writers()
    for p in patchers:
        p.start()
    try:
        with pytest.raises(MarkPendingError) as exc:
            mark_pending(
                stage="hand_starts",
                video_id=VIDEO_ID,
                project=PROJECT,
                dataset=DATASET,
                only_ids=["other_video_001_001"],
                client=client,
            )
    finally:
        for p in patchers:
            p.stop()
    assert "other_video_001_001" in str(exc.value)
    for mock in mocks.values():
        mock.assert_not_called()
    assert not any(
        c.args[0].lstrip().startswith("DELETE") for c in client.query.call_args_list
    )


def test_a_nonexistent_id_fails_the_whole_command():
    client = _mock_bq(_handler(entities=[("vid001_001_001", "complete", 1)]))
    with pytest.raises(MarkPendingError) as exc:
        mark_pending(
            stage="hand_starts",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            only_ids=["vid001_001_001", "vid001_009_typo"],
            client=client,
        )
    assert "vid001_009_typo" in str(exc.value)
    assert "vid001_001_001" not in str(exc.value)


def test_status_filters_the_entity_set():
    client = _mock_bq(_handler(entities=[("vid001_001_001", "failed_parked", 0)]))
    patchers, _ = _patch_writers()
    for p in patchers:
        p.start()
    try:
        mark_pending(
            stage="hand_starts",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            only_statuses=["failed_parked"],
            client=client,
        )
    finally:
        for p in patchers:
            p.stop()
    entity_call = [c for c in client.query.call_args_list if "output_counts AS" in c.args[0]][0]
    assert "a.latest_status IN UNNEST(@only_statuses)" in entity_call.args[0]
    names = {p.name for p in entity_call.kwargs["job_config"].query_parameters}
    assert names == {"video_id", "only_statuses"}


def test_no_filters_binds_only_the_video():
    sql = entity_sql(
        type("S", (), {
            "key_column": "hand_setup_id",
            "attempts_table": "hand_setup_processing_attempts",
            "output_table": "hand_starts",
            "input_table": "hand_setups",
        })(),
        PROJECT,
        DATASET,
        by_id=False,
        by_status=False,
    )
    assert "only_ids" not in sql
    assert "only_statuses" not in sql


# --- the cascade ---


@pytest.mark.parametrize("stage", STAGES)
def test_the_named_stage_is_never_deleted(stage):
    """Replace semantics overwrite the named stage's rows on the next run, and a
    DELETE up front would leave an entity with zero rows if processing failed."""
    plan, _, _ = _run(stage, entities=[("e1", "complete", 1)])
    assert stage not in [table for table, _ in plan.deletes]


def test_deletes_are_child_before_parent():
    plan, _, _ = _run("clip_manifest", entities=[(VIDEO_ID, "complete", 3)])
    assert [t for t, _ in plan.deletes] == ["hand_actions", "hand_starts", "hand_setups"]


def test_an_id_narrows_the_cascade_not_just_the_mark():
    client = _mock_bq(_handler(entities=[("vid001_001", "complete", 4)]))
    patchers, _ = _patch_writers()
    for p in patchers:
        p.start()
    try:
        mark_pending(
            stage="hand_setups",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            only_ids=["vid001_001"],
            client=client,
        )
    finally:
        for p in patchers:
            p.stop()
    deletes = [c for c in client.query.call_args_list if c.args[0].lstrip().startswith("DELETE")]
    assert deletes
    for call in deletes:
        assert "AND clip_id IN UNNEST(@scope_ids)" in call.args[0]
        names = {p.name for p in call.kwargs["job_config"].query_parameters}
        assert names == {"video_id", "scope_ids"}


def test_an_unnarrowed_run_deletes_by_video_id_alone():
    """Sweeps orphaned downstream rows an id list could never reach."""
    client = _mock_bq(_handler(entities=[("vid001_001", "complete", 4)]))
    patchers, _ = _patch_writers()
    for p in patchers:
        p.start()
    try:
        mark_pending(
            stage="hand_setups",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            client=client,
        )
    finally:
        for p in patchers:
            p.stop()
    deletes = [c for c in client.query.call_args_list if c.args[0].lstrip().startswith("DELETE")]
    assert deletes
    for call in deletes:
        assert "scope_ids" not in call.args[0]


def test_marks_cover_the_named_stage_and_everything_downstream():
    plan, _, mocks = _run(
        "hand_setups",
        entities=[("vid001_001", "complete", 4)],
        mark_ids={"hand_setups": ["hs1", "hs2"], "hand_starts": ["st1"]},
    )
    assert plan.marks == [
        ("clip_processing_attempts", 1),
        ("hand_setup_processing_attempts", 2),
        ("hand_start_processing_attempts", 1),
    ]
    mocks["clip_materialization_attempts"].assert_not_called()
    mocks["tournament_results_processing_attempts"].assert_not_called()


def test_marks_are_written_as_failed_transient_with_a_greppable_message():
    _, _, mocks = _run(
        "hand_starts",
        entities=[("vid001_001_001", "complete", 1)],
        mark_ids={"hand_starts": ["vid001_001_001_001"]},
    )
    rows = mocks["hand_setup_processing_attempts"].call_args.args[0]
    assert [r.status for r in rows] == ["failed_transient"]
    assert rows[0].status_message == "mark-pending: rebuilding hand_starts"
    assert rows[0].hand_setup_id == "vid001_001_001"
    assert rows[0].attempt_id


def test_clip_processing_attempt_marks_omit_attempt_id():
    """clip_processing_attempts is the one attempts table without one."""
    _, _, mocks = _run("hand_setups", entities=[("vid001_001", "complete", 0)])
    rows = mocks["clip_processing_attempts"].call_args.args[0]
    assert rows[0].clip_id == "vid001_001"
    assert not hasattr(rows[0], "attempt_id")


# --- dry run ---


def test_dry_run_writes_nothing():
    client = _mock_bq(_handler(entities=[("vid001_001_001", "complete", 1)]))
    patchers, mocks = _patch_writers()
    for p in patchers:
        p.start()
    try:
        mark_pending(
            stage="hand_starts",
            video_id=VIDEO_ID,
            project=PROJECT,
            dataset=DATASET,
            dry_run=True,
            client=client,
        )
    finally:
        for p in patchers:
            p.stop()
    for mock in mocks.values():
        mock.assert_not_called()
    assert not any(
        c.args[0].lstrip().startswith("DELETE") for c in client.query.call_args_list
    )


def test_build_plan_is_the_same_with_and_without_dry_run():
    entities = [("vid001_001_001", "complete", 1)]
    mark_ids = {"hand_starts": ["vid001_001_001_001"]}
    dry, _, _ = _run("hand_starts", entities=entities, mark_ids=mark_ids, dry_run=True)
    wet, _, _ = _run("hand_starts", entities=entities, mark_ids=mark_ids)
    assert dry == wet


# --- execution order ---


def test_all_deletes_precede_all_marks():
    log = []
    _run(
        "clip_manifest",
        entities=[(VIDEO_ID, "complete", 3)],
        mark_ids={"clip_manifest": ["c1"], "hand_setups": ["h1"], "hand_starts": ["s1"]},
        log=log,
    )
    kinds = [
        ("delete" if e[1].lstrip().startswith("DELETE") else "read") if e[0] == "query"
        else "mark"
        for e in log
    ]
    deletes = [i for i, k in enumerate(kinds) if k == "delete"]
    marks = [i for i, k in enumerate(kinds) if k == "mark"]
    assert deletes and marks
    assert max(deletes) < min(marks)


def test_deletes_execute_child_before_parent():
    log = []
    _run("clip_manifest", entities=[(VIDEO_ID, "complete", 3)], log=log)
    ordered = [
        _table_of(sql)
        for kind, sql in [(e[0], e[1]) for e in log if e[0] == "query"]
        if sql.lstrip().startswith("DELETE")
    ]
    assert ordered == ["hand_actions", "hand_starts", "hand_setups"]


# --- report ---


def _plan_with(n_entities, status="complete", rows=1):
    entities = [(f"vid001_001_{i:03d}", status, rows) for i in range(n_entities)]
    plan, _, _ = _run("hand_starts", entities=entities, dry_run=True)
    return plan


def test_ids_list_individually_at_the_threshold():
    out = format_plan(_plan_with(DETAIL_THRESHOLD), dry_run=True)
    assert "vid001_001_000" in out
    assert "current status of marked entities" not in out


def test_ids_collapse_to_a_histogram_above_the_threshold():
    out = format_plan(_plan_with(DETAIL_THRESHOLD + 1), dry_run=True)
    assert "current status of marked entities" in out
    assert "vid001_001_000" not in out
    assert "complete" in out


def test_report_names_the_entity_type():
    out = format_plan(_plan_with(1), dry_run=True)
    assert "Rebuilding hand_starts. Marking 1 hand_setups entity pending" in out


def test_zero_row_entities_are_reported_as_by_design():
    out = format_plan(_plan_with(1, status="complete_uncontested", rows=0), dry_run=True)
    assert "1 of these have no hand_starts row (1 complete_uncontested)" in out


def test_never_attempted_entities_are_not_flagged_as_missing_output():
    plan = _plan_with(1, status=None, rows=0)
    assert plan.missing_output == []


def test_dry_run_and_real_run_use_different_verbs():
    plan = _plan_with(1)
    assert "WOULD DELETE" in format_plan(plan, dry_run=True)
    assert "DELETED" in format_plan(plan, dry_run=False)
    assert "WOULD" not in format_plan(plan, dry_run=False)


def test_leaf_stage_reports_nothing_to_delete():
    plan, _, _ = _run("hand_actions", entities=[("h1", "complete", 1)], dry_run=True)
    assert plan.deletes == []
    assert "nothing — no stage is downstream of hand_actions" in format_plan(
        plan, dry_run=True
    )


def test_empty_entity_set_says_so():
    plan, _, _ = _run("hand_starts", entities=[], dry_run=True)
    assert "nothing matched" in format_plan(plan, dry_run=True)


def test_call_estimate_counts_every_marked_phase():
    plan, _, _ = _run(
        "hand_starts",
        entities=[("a", "complete", 1), ("b", "complete", 1)],
        mark_ids={"hand_starts": ["a_001"]},
        dry_run=True,
    )
    # 2 hand_setups x 2 calls + 1 hand_start x 3.4 calls
    assert plan.estimated_calls == round(2 * 2 + 1 * 3.4)
    assert "Gemini calls" in format_plan(plan, dry_run=True)


def test_materialization_marks_report_no_llm_calls():
    plan, _, _ = _run("clip_manifest", entities=[(VIDEO_ID, "complete", 3)], dry_run=True)
    out = format_plan(plan, dry_run=True)
    assert "clip_materialization_attempts" in out
    assert "(no LLM calls)" in out


def test_speculative_marks_are_explained_in_the_output():
    plan, _, _ = _run("hand_setups", entities=[("c1", "complete", 4)], dry_run=True)
    out = format_plan(plan, dry_run=True)
    assert "re-detection will not reproduce" in out


# --- integration tests ---

_INTEGRATION_PROJECT = "table-talk-497020"
_INTEGRATION_DATASET = "table_talk_dev"


@dataclass(frozen=True)
class _Ids:
    video_id: str
    clip_id: str
    hand_setup_id: str
    hand_start_id: str


def _ids():
    video_id = f"test_mp_{uuid.uuid4().hex[:10]}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    return _Ids(video_id, clip_id, hand_setup_id, f"{hand_setup_id}_001")


def _seed(ids, client):
    """A complete Phase 1-5 fixture, built with the earlier phases' production
    writers. Writers, not orchestrators, per CLAUDE.md's cross-phase setup rule.
    """
    project, dataset = _INTEGRATION_PROJECT, _INTEGRATION_DATASET
    write_video_row(
        VideosRow(
            video_id=ids.video_id,
            source_url=f"https://www.youtube.com/watch?v={ids.video_id}",
            title="mark-pending Test Video",
            duration_seconds=240,
            gcs_path=f"gs://{project}-videos-dev/{ids.video_id}.mp4",
            file_size_bytes=1234,
        ),
        project=project,
        dataset=dataset,
        client=client,
    )
    # Phase 3's pending query joins tournament_results for bounty_type and raises
    # on a null. In production the materialization gate guarantees this row
    # precedes any clip; a fixture written directly must supply it.
    write_tournament_results(
        [
            TournamentResultsRow(
                video_id=ids.video_id,
                bounty_type="none",
                currency_symbol="$",
                frame_timestamp_seconds=5,
                frame_gcs_path=(
                    f"gs://{project}-tournament-results-dev/{ids.video_id}.jpg"
                ),
                tournament_results_state={"payouts": []},
            )
        ],
        video_id=ids.video_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )
    write_clip_manifest_rows(
        [
            ClipManifestRow(
                clip_id=ids.clip_id,
                video_id=ids.video_id,
                clip_start_time=0,
                clip_end_time=240,
            )
        ],
        video_id=ids.video_id,
        project=project,
        dataset=dataset,
        client=client,
    )
    write_hand_setups(
        [
            HandSetupsRow(
                hand_setup_id=ids.hand_setup_id,
                clip_id=ids.clip_id,
                video_id=ids.video_id,
                hand_setup_time_seconds=10,
                frame_gcs_path=f"gs://{project}-hand-setups-dev/{ids.hand_setup_id}.jpg",
                hand_setup_state={"seats": []},
            )
        ],
        clip_id=ids.clip_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )
    write_hand_starts(
        [
            HandStartsRow(
                hand_start_id=ids.hand_start_id,
                hand_setup_id=ids.hand_setup_id,
                clip_id=ids.clip_id,
                video_id=ids.video_id,
                fva_time_seconds=20,
                second_action_time_seconds=24,
                hand_start_state={"fva": {}},
                fva_frame_gcs_path=f"gs://{project}-hand-starts-dev/{ids.hand_start_id}.jpg",
                verify_frame_gcs_paths=[],
            )
        ],
        hand_setup_id=ids.hand_setup_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )
    write_hand_actions(
        [
            HandActionsRow(
                hand_start_id=ids.hand_start_id,
                hand_setup_id=ids.hand_setup_id,
                clip_id=ids.clip_id,
                video_id=ids.video_id,
                hand_action_state={"streets": []},
                street_frame_gcs_paths=[],
            )
        ],
        hand_start_id=ids.hand_start_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )
    # Terminal successes, so nothing is pending before the mark.
    write_clip_processing_attempt_row(
        ClipProcessingAttemptsRow(clip_id=ids.clip_id, status="complete"),
        project=project,
        dataset=dataset,
        client=client,
    )
    write_hand_setup_processing_attempt_row(
        HandSetupProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex, hand_setup_id=ids.hand_setup_id, status="complete"
        ),
        project=project,
        dataset=dataset,
        client=client,
    )
    write_hand_start_processing_attempt_row(
        HandStartProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex, hand_start_id=ids.hand_start_id, status="complete"
        ),
        project=project,
        dataset=dataset,
        client=client,
    )


def _count(client, table, column, value):
    sql = (
        f"SELECT COUNT(*) AS n FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.{table}` "
        f"WHERE {column} = @v"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("v", "STRING", value)]
    )
    return list(client.query(sql, job_config=job_config).result())[0].n


def _cleanup(client, ids):
    """Reverse dependency order: deepest stage table first, videos last."""
    for table, column, value in (
        ("hand_actions", "hand_start_id", ids.hand_start_id),
        ("hand_start_processing_attempts", "hand_start_id", ids.hand_start_id),
        ("hand_starts", "hand_setup_id", ids.hand_setup_id),
        ("hand_setup_processing_attempts", "hand_setup_id", ids.hand_setup_id),
        ("hand_setups", "clip_id", ids.clip_id),
        ("clip_processing_attempts", "clip_id", ids.clip_id),
        ("clip_manifest", "video_id", ids.video_id),
        ("tournament_results", "video_id", ids.video_id),
        ("videos", "video_id", ids.video_id),
    ):
        client.query(
            f"DELETE FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.{table}` "
            f"WHERE {column} = @v",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("v", "STRING", value)]
            ),
        ).result()


@pytest.mark.integration
def test_mark_pending_hand_setups_cycle_integration():
    from table_talk.hand_setup_processing import _find_pending_clips

    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)

        # Nothing is pending before the mark: the clip's latest status is complete.
        before = _find_pending_clips(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_clip_ids=[ids.clip_id],
            client=client,
        )
        assert before == []

        plan = mark_pending(
            stage="hand_setups",
            video_id=ids.video_id,
            project=_INTEGRATION_PROJECT,
            dataset=_INTEGRATION_DATASET,
            client=client,
        )
        assert plan.entities == [(ids.clip_id, "complete")]
        assert [t for t, _ in plan.deletes] == ["hand_actions", "hand_starts"]

        # Downstream rows gone.
        assert _count(client, "hand_actions", "video_id", ids.video_id) == 0
        assert _count(client, "hand_starts", "video_id", ids.video_id) == 0
        # The named stage's own rows untouched.
        assert _count(client, "hand_setups", "video_id", ids.video_id) == 1
        assert _count(client, "clip_manifest", "video_id", ids.video_id) == 1

        # Attempt rows appended at every marked level.
        assert _count(client, "clip_processing_attempts", "clip_id", ids.clip_id) == 2
        assert (
            _count(
                client,
                "hand_setup_processing_attempts",
                "hand_setup_id",
                ids.hand_setup_id,
            )
            == 2
        )
        assert (
            _count(
                client, "hand_start_processing_attempts", "hand_start_id", ids.hand_start_id
            )
            == 2
        )

        # The end-to-end proof: the real pending query now selects the clip.
        after = _find_pending_clips(
            _INTEGRATION_PROJECT,
            _INTEGRATION_DATASET,
            only_clip_ids=[ids.clip_id],
            client=client,
        )
        assert [c.clip_id for c in after] == [ids.clip_id]
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_mark_pending_dry_run_changes_nothing_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)

        plan = mark_pending(
            stage="hand_setups",
            video_id=ids.video_id,
            project=_INTEGRATION_PROJECT,
            dataset=_INTEGRATION_DATASET,
            dry_run=True,
            client=client,
        )
        assert plan.deletes == [("hand_actions", 1), ("hand_starts", 1)]

        assert _count(client, "hand_actions", "video_id", ids.video_id) == 1
        assert _count(client, "hand_starts", "video_id", ids.video_id) == 1
        assert _count(client, "hand_setups", "video_id", ids.video_id) == 1
        assert _count(client, "clip_processing_attempts", "clip_id", ids.clip_id) == 1
        assert (
            _count(
                client,
                "hand_setup_processing_attempts",
                "hand_setup_id",
                ids.hand_setup_id,
            )
            == 1
        )
    finally:
        _cleanup(client, ids)
