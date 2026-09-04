import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

from table_talk._generated.clip_manifest_row import ClipManifestRow
from table_talk._generated.clip_materialization_attempts_row import (
    ClipMaterializationAttemptsRow,
)
from table_talk._generated.hand_setup_processing_attempts_row import (
    HandSetupProcessingAttemptsRow,
)
from table_talk._generated.hand_setups_row import HandSetupsRow
from table_talk._generated.hand_starts_row import HandStartsRow
from table_talk.clip_manifest_writer import write_clip_manifest_rows
from table_talk.clip_materialization_attempts_writer import (
    write_clip_materialization_attempt_row,
)
from table_talk.hand_setup_processing_attempts_writer import (
    write_hand_setup_processing_attempt_row,
)
from table_talk.hand_setups_writer import write_hand_setups
from table_talk.hand_starts_writer import write_hand_starts
from table_talk.integrity import (
    DETAIL_THRESHOLD,
    DUPLICATE_KEYS,
    PHASES,
    Finding,
    IntegrityReport,
    duplicate_sql,
    format_report,
    known_video_sql,
    orphan_sql,
    row_count_sql,
    run_integrity_checks,
    status_row_sql,
)
from table_talk.videos_writer import VideosRow, write_video_row

PROJECT = "test-project"
DATASET = "test_dataset"

_SPECS = {spec.label: spec for spec in PHASES}


def _mock_bq(handler):
    """A BQ client whose query() dispatches on SQL content.

    Keyed on substrings rather than call order, so a test does not break when an
    unrelated check is added to the runner.
    """
    def query(sql, job_config=None):
        job = MagicMock()
        job.result.return_value = handler(sql)
        return job

    mock = MagicMock()
    mock.query.side_effect = query
    return mock


def _row(**kwargs):
    mock = MagicMock()
    for key, value in kwargs.items():
        setattr(mock, key, value)
    return mock


def _counts_rows():
    return [_row(table_name="videos", n=1)]


def _empty_runner(extra=None):
    """Handler returning no findings, with `extra` overriding by SQL substring."""
    extra = extra or []

    def handler(sql):
        if "UNION ALL" in sql:
            return _counts_rows()
        for needle, rows in extra:
            if needle in sql:
                return rows
        return []

    return handler


# --- query builder shape ---


def test_orphan_sql_is_an_anti_join_on_the_parent_key():
    sql = orphan_sql(_SPECS["Phase 4"], PROJECT, DATASET, scoped=False)
    assert f"FROM `{PROJECT}.{DATASET}.hand_starts` c" in sql
    assert f"LEFT JOIN `{PROJECT}.{DATASET}.hand_setups` par" in sql
    assert "ON c.hand_setup_id = par.hand_setup_id" in sql
    assert "WHERE par.hand_setup_id IS NULL" in sql
    assert "SELECT *" not in sql


def test_duplicate_sql_groups_and_filters_on_count():
    sql = duplicate_sql("hand_starts", "hand_setup_id", PROJECT, DATASET, scoped=False)
    assert "GROUP BY t.hand_setup_id" in sql
    assert "HAVING COUNT(*) > 1" in sql
    assert "SELECT *" not in sql


def test_status_row_sql_uses_the_shared_latest_status_idiom():
    sql = status_row_sql(_SPECS["Phase 5"], PROJECT, DATASET, scoped=False)
    assert "ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)]" in sql
    assert f"FROM `{PROJECT}.{DATASET}.hand_start_processing_attempts`" in sql
    # Anchored on the input table, not the attempts table — that is what carries
    # video_id and what makes scoping possible.
    assert f"FROM `{PROJECT}.{DATASET}.hand_starts` i" in sql
    assert "SELECT *" not in sql


def test_status_row_sql_encodes_each_phases_arity():
    phase4 = status_row_sql(_SPECS["Phase 4"], PROJECT, DATASET, scoped=False)
    assert "IN ('complete_skipped', 'complete_uncontested') AND COALESCE(o.row_count, 0) != 0" in (
        " ".join(phase4.split())
    )
    assert "IN ('complete') AND COALESCE(o.row_count, 0) != 1" in " ".join(phase4.split())

    # Phase 2's complete produces one clip per window, so >= 1, not exactly 1.
    phase2 = " ".join(status_row_sql(_SPECS["Phase 2"], PROJECT, DATASET, scoped=False).split())
    assert "IN ('complete') AND COALESCE(o.row_count, 0) < 1" in phase2
    assert "IN ('blocked_upstream') AND COALESCE(o.row_count, 0) != 0" in phase2


def test_failure_statuses_are_never_constrained():
    """The regression guard for the false positive most likely to be introduced.

    A stage row legitimately coexists with failed_transient (the outage path) or
    failed_parked. Flagging those would fire on exactly the incident this tool
    exists to catch.
    """
    for spec in PHASES:
        assert not any(status.startswith("failed") for status in spec.arity)
        if not spec.arity:
            continue
        sql = status_row_sql(spec, PROJECT, DATASET, scoped=False)
        assert "failed_transient" not in sql
        assert "failed_parked" not in sql
        assert "failed_permanent" not in sql


def test_phase_3_has_no_status_row_invariant():
    assert _SPECS["Phase 3"].arity == {}
    with pytest.raises(ValueError):
        status_row_sql(_SPECS["Phase 3"], PROJECT, DATASET, scoped=False)


def test_orphan_check_skips_phases_whose_parent_is_videos():
    parents = {spec.input_table for spec in PHASES if spec.input_table != "videos"}
    assert parents == {"clip_manifest", "hand_setups", "hand_starts"}


def test_scope_filter_present_only_when_scoped():
    for builder in (
        lambda s: orphan_sql(_SPECS["Phase 4"], PROJECT, DATASET, scoped=s),
        lambda s: duplicate_sql("hand_starts", "hand_setup_id", PROJECT, DATASET, scoped=s),
        lambda s: status_row_sql(_SPECS["Phase 4"], PROJECT, DATASET, scoped=s),
        lambda s: row_count_sql(PROJECT, DATASET, scoped=s),
    ):
        assert "IN UNNEST(@only_video_ids)" in builder(True)
        assert "IN UNNEST(@only_video_ids)" not in builder(False)


def test_known_video_sql_reads_only_the_id_column():
    sql = known_video_sql(PROJECT, DATASET)
    assert "SELECT v.video_id AS video_id" in sql
    assert "IN UNNEST(@only_video_ids)" in sql


# --- the mapping is bound to the writers ---


def test_every_writer_status_is_constrained_or_deliberately_exempt():
    """Adding a status and forgetting `arity` must fail loudly.

    Without this, a new status is silently unconstrained and its check quietly
    stops covering it.
    """
    from table_talk.clip_materialization_attempts_writer import (
        VALID_STATUSES as PHASE2,
    )
    from table_talk.clip_processing_attempts_writer import VALID_STATUSES as PHASE3
    from table_talk.hand_setup_processing_attempts_writer import (
        VALID_STATUSES as PHASE4,
    )
    from table_talk.hand_start_processing_attempts_writer import (
        VALID_STATUSES as PHASE5,
    )
    from table_talk.integrity import UNCONSTRAINED
    from table_talk.tournament_results_processing_attempts_writer import (
        VALID_STATUSES as PAYOUTS,
    )

    valid = {
        "Payouts": PAYOUTS,
        "Phase 2": PHASE2,
        "Phase 3": PHASE3,
        "Phase 4": PHASE4,
        "Phase 5": PHASE5,
    }
    for spec in PHASES:
        writer_statuses = valid[spec.label]
        failures = {s for s in writer_statuses if s.startswith("failed")}
        covered = set(spec.arity) | failures | set(UNCONSTRAINED.get(spec.label, frozenset()))
        assert covered == set(writer_statuses), spec.label
        # Nothing in the map may name a status the writer cannot produce.
        assert set(spec.arity) <= set(writer_statuses), spec.label


def test_duplicate_keys_cover_every_stage_table():
    assert set(DUPLICATE_KEYS) == {spec.output_table for spec in PHASES}


# --- runner ---


def test_unknown_video_id_is_reported():
    bq = _mock_bq(_empty_runner(extra=[("FROM `test-project.test_dataset.videos` v", [])]))

    report = run_integrity_checks(
        project=PROJECT, dataset=DATASET, only_video_ids=["nope"], client=bq
    )

    assert [f.check for f in report.findings] == ["unknown_video"]
    assert report.findings[0].entity_id == "nope"
    assert "no such video" in format_report(report)


def test_known_video_id_is_not_reported_as_unknown():
    bq = _mock_bq(
        _empty_runner(
            extra=[("FROM `test-project.test_dataset.videos` v", [_row(video_id="abc")])]
        )
    )

    report = run_integrity_checks(
        project=PROJECT, dataset=DATASET, only_video_ids=["abc"], client=bq
    )

    assert report.findings == []


def test_empty_only_video_ids_scopes_to_nothing_not_everything():
    bq = _mock_bq(_empty_runner())

    run_integrity_checks(project=PROJECT, dataset=DATASET, only_video_ids=[], client=bq)

    for call in bq.query.call_args_list:
        sql = call[0][0]
        job_config = call.kwargs["job_config"]
        assert "IN UNNEST(@only_video_ids)" in sql
        params = {p.name: p for p in job_config.query_parameters}
        assert params["only_video_ids"].values == []


def test_corpus_wide_run_passes_no_query_parameters():
    bq = _mock_bq(_empty_runner())

    run_integrity_checks(project=PROJECT, dataset=DATASET, client=bq)

    for call in bq.query.call_args_list:
        assert call.kwargs.get("job_config") is None


def test_status_row_finding_names_the_expected_arity():
    hit = _row(entity_id="v_003_002", video_id="v", latest_status="complete", row_count=0)
    bq = _mock_bq(_empty_runner(extra=[("hand_start_processing_attempts", [hit])]))

    report = run_integrity_checks(project=PROJECT, dataset=DATASET, client=bq)

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.check == "status_row"
    assert finding.table == "hand_actions"
    assert finding.detail == "complete → expected 1 row, found 0"


def test_orphan_finding_names_the_missing_parent():
    hit = _row(entity_id="v_003_002", video_id="v")
    needle = "LEFT JOIN `test-project.test_dataset.hand_setups` par"
    bq = _mock_bq(_empty_runner(extra=[(needle, [hit])]))

    report = run_integrity_checks(project=PROJECT, dataset=DATASET, client=bq)

    assert [f.check for f in report.findings] == ["orphan"]
    assert report.findings[0].table == "hand_starts"
    assert "no hand_setups row" in report.findings[0].detail


# --- report formatting ---


def _findings(n, check="orphan"):
    return [
        Finding(
            check=check,
            table="hand_starts",
            entity_id=f"vid_{i:03d}",
            video_id="vid",
            detail="no hand_setups row for hand_setup_id",
        )
        for i in range(n)
    ]


def _report(findings, scope=None):
    return IntegrityReport(
        videos_checked=1, stage_rows=110, findings=findings, scope=scope
    )


def test_clean_report_says_clean_and_names_phase_3s_absent_invariant():
    out = format_report(_report([]))
    assert "clean." in out
    assert "orphaned rows                  0" in out
    assert "hand_setups" in out
    assert "row count cannot be predicted" in out


def test_invariant_block_prints_even_when_there_are_findings():
    out = format_report(_report(_findings(1)))
    assert "status/row invariants, by phase:" in out
    assert "failure statuses are unconstrained in every phase" in out


def test_invariant_block_names_each_phases_arity():
    out = format_report(_report([]))
    assert "clip_manifest       complete → >= 1 row; blocked_upstream → 0 rows" in out
    assert "hand_actions        complete → 1 row; complete_skipped → 0 rows" in out
    assert (
        "hand_starts         complete → 1 row; "
        "complete_skipped, complete_uncontested → 0 rows" in out
    )


def test_at_threshold_findings_are_listed_individually():
    out = format_report(_report(_findings(DETAIL_THRESHOLD)))
    assert f"orphaned rows ({DETAIL_THRESHOLD})" in out
    assert "vid_000" in out
    assert "vid_019" in out
    assert "run with --video-id" not in out


def test_above_threshold_findings_collapse_to_counts():
    out = format_report(_report(_findings(DETAIL_THRESHOLD + 1)))
    assert "vid_000" not in out
    assert "affected: vid" in out
    assert f"{DETAIL_THRESHOLD + 1} findings — run with --video-id for per-entity detail" in out


def test_scoped_header_echoes_the_scope():
    out = format_report(_report([], scope=("abc", "def")))
    assert out.startswith("check-integrity --video-id abc --video-id def — 1 video, 110 stage rows")


def test_orphan_counts_break_out_per_table():
    findings = _findings(2) + [
        Finding(
            check="orphan",
            table="hand_actions",
            entity_id="x",
            video_id="vid",
            detail="no hand_starts row for hand_start_id",
        )
    ]
    out = format_report(_report(findings))
    assert "orphaned hand_starts" in out
    assert "orphaned hand_actions" in out


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
    video_id = f"test_{uuid.uuid4().hex[:10]}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    return _Ids(video_id, clip_id, hand_setup_id, f"{hand_setup_id}_001")


def _seed(ids, client, *, with_hand_start=True):
    """Build a clean fixture with the earlier phases' production writers.

    Writers, not orchestrators, per CLAUDE.md's cross-phase setup rule — an
    orchestrator would download the video and call Gemini.
    """
    project, dataset = _INTEGRATION_PROJECT, _INTEGRATION_DATASET
    write_video_row(
        VideosRow(
            video_id=ids.video_id,
            source_url=f"https://www.youtube.com/watch?v={ids.video_id}",
            title="Integrity Test Video",
            duration_seconds=240,
            gcs_path=f"gs://{project}-videos-dev/{ids.video_id}.mp4",
            file_size_bytes=1234,
        ),
        project=project,
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
    if with_hand_start:
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


def _seed_phase4_attempt(ids, status, client):
    write_hand_setup_processing_attempt_row(
        HandSetupProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_setup_id=ids.hand_setup_id,
            status=status,
            status_message=f"integrity integration seed: {status}",
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=client,
    )


def _seed_phase2_attempt(ids, status, client):
    write_clip_materialization_attempt_row(
        ClipMaterializationAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            video_id=ids.video_id,
            status=status,
            status_message=f"integrity integration seed: {status}",
        ),
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        client=client,
    )


def _delete(client, table, column, value):
    client.query(
        f"DELETE FROM `{_INTEGRATION_PROJECT}.{_INTEGRATION_DATASET}.{table}` "
        f"WHERE {column} = @v",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("v", "STRING", value)]
        ),
    ).result()


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
        ("clip_materialization_attempts", "video_id", ids.video_id),
        ("videos", "video_id", ids.video_id),
    ):
        _delete(client, table, column, value)


def _check(ids, client):
    """Always scoped to the ids this test owns — never corpus-wide."""
    return run_integrity_checks(
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        only_video_ids=[ids.video_id],
        client=client,
    )


@pytest.mark.integration
def test_clean_fixture_reports_clean_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)
        _seed_phase2_attempt(ids, "complete", client)
        _seed_phase4_attempt(ids, "complete", client)

        report = _check(ids, client)

        assert report.findings == []
        assert report.videos_checked == 1
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_orphaned_hand_start_is_detected_and_named_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)
        _seed_phase4_attempt(ids, "complete", client)
        # Orphan it: the parent hand_setups row goes, the hand_starts row stays.
        # This is Phase 3 re-detection shrinkage in miniature.
        _delete(client, "hand_setups", "hand_setup_id", ids.hand_setup_id)

        report = _check(ids, client)

        orphans = [f for f in report.findings if f.check == "orphan"]
        assert len(orphans) == 1
        assert orphans[0].table == "hand_starts"
        assert orphans[0].entity_id == ids.hand_setup_id
        assert ids.hand_setup_id in format_report(report)
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_complete_with_zero_rows_is_detected_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client, with_hand_start=False)
        _seed_phase4_attempt(ids, "complete", client)

        report = _check(ids, client)

        mismatches = [f for f in report.findings if f.check == "status_row"]
        assert len(mismatches) == 1
        assert mismatches[0].entity_id == ids.hand_setup_id
        assert mismatches[0].detail == "complete → expected 1 row, found 0"
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_complete_with_one_row_is_not_detected_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)
        _seed_phase4_attempt(ids, "complete", client)

        report = _check(ids, client)

        assert [f for f in report.findings if f.check == "status_row"] == []
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_row_coexisting_with_failed_status_is_not_flagged_integration():
    """The outage path, end to end: a write that succeeded while the client's
    confirmation failed leaves a good row under a failure status. Flagging it
    would fire on exactly the incident this tool exists to catch."""
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    ids = _ids()
    try:
        _seed(ids, client)
        _seed_phase4_attempt(ids, "complete", client)
        _seed_phase4_attempt(ids, "failed_transient", client)

        assert _check(ids, client).findings == []

        _seed_phase4_attempt(ids, "failed_parked", client)

        assert _check(ids, client).findings == []
    finally:
        _cleanup(client, ids)


@pytest.mark.integration
def test_unknown_video_id_is_reported_integration():
    client = bigquery.Client(project=_INTEGRATION_PROJECT)
    report = run_integrity_checks(
        project=_INTEGRATION_PROJECT,
        dataset=_INTEGRATION_DATASET,
        only_video_ids=[f"test_absent_{uuid.uuid4().hex[:10]}"],
        client=client,
    )
    assert [f.check for f in report.findings] == ["unknown_video"]
