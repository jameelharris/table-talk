# Read-only corpus audit behind `tt check-integrity`. Deletes nothing, writes
# nothing, always exits zero.
#
# This answers "is anything currently wrong?" across the corpus. It is the
# complement of a `--dry-run`, which answers "if I proceed, what changes?" for
# named entities: a stale row from a reprocess weeks ago never surfaces in a
# dry-run unless that same entity happens to be named again.
#
# Scope is correctness, not progress. A video part-way through the pipeline is
# incomplete, not inconsistent, and must report clean — an audit tool that is
# noisy on its first run is one nobody runs.
#
# Not an orchestrator: it consults BigQuery and returns findings, and composes
# no primitives.
#
# COST: BigQuery bills on bytes scanned and is columnar. Every query here
# touches only id, status and timestamp columns and never the JSON blobs, which
# are nearly all the data. A `SELECT *` anywhere in this module would turn a
# near-free query into a full-table scan at corpus scale.

from dataclasses import dataclass

from google.cloud import bigquery

# Findings listed individually up to this many; above it the report collapses to
# counts. The threshold keys on the number of findings, not the number of videos
# — one video with 60 orphans is worse output than five videos with one each.
DETAIL_THRESHOLD = 20


@dataclass(frozen=True)
class PhaseSpec:
    """One phase's input -> attempts -> output triple.

    Anchoring on `input_table` rather than `attempts_table` is what makes the
    stage-to-attempts off-by-one tractable: an attempts table sits *between* the
    entity a phase consumes and the rows it produces, and is named for the
    former. So `hand_setup_processing_attempts` belongs to the phase that
    *produces* `hand_starts`.

    Two further properties fall out of anchoring on the input table:

    - Every input table carries `video_id`, while `clip_processing_attempts`,
      `hand_setup_processing_attempts` and `hand_start_processing_attempts` do
      not. This is what makes `--video-id` scoping possible at all.
    - An attempt row whose entity no longer exists (Phase 3 re-detection
      shrinkage) drops out rather than being flagged. Correct: state tables are
      append-only audit logs, and a superseded entity's history is not an
      anomaly.

    `key_column` names the same column in all three tables.

    `arity` maps a status to the row count it implies. A status absent from the
    map is unconstrained — which is how the failure statuses are tolerated. A
    stage row may legitimately coexist with `failed_transient` (the outage path:
    the write succeeded, the client's confirmation failed) or `failed_parked` (an
    entity that completed, later failed three times, and parked). Flagging those
    would produce false positives on exactly the incident this tool exists to
    catch, so they are never listed rather than being excluded by a clause.
    """

    label: str
    input_table: str
    key_column: str
    attempts_table: str
    output_table: str
    arity: dict[str, str]


# Arity differs by phase; there is no single "complete means one row" rule.
# Phase 2's complete produces one clip per 240s window, Phase 3's cannot be
# predicted from status at all, and the rest are exactly one.
PHASES: tuple[PhaseSpec, ...] = (
    PhaseSpec(
        label="Payouts",
        input_table="videos",
        key_column="video_id",
        attempts_table="tournament_results_processing_attempts",
        output_table="tournament_results",
        arity={"complete": "one", "complete_skipped": "zero"},
    ),
    PhaseSpec(
        label="Phase 2",
        input_table="videos",
        key_column="video_id",
        attempts_table="clip_materialization_attempts",
        output_table="clip_manifest",
        arity={"complete": "at_least_one", "blocked_upstream": "zero"},
    ),
    PhaseSpec(
        # Empty arity: a clip that legitimately detects zero hand setups is
        # `complete` with zero rows, so row count cannot be predicted from
        # status. Only orphans and duplicates apply here. This is a property of
        # the phase, not a gap in the tool, and the report says so rather than
        # silently omitting it.
        label="Phase 3",
        input_table="clip_manifest",
        key_column="clip_id",
        attempts_table="clip_processing_attempts",
        output_table="hand_setups",
        arity={},
    ),
    PhaseSpec(
        label="Phase 4",
        input_table="hand_setups",
        key_column="hand_setup_id",
        attempts_table="hand_setup_processing_attempts",
        output_table="hand_starts",
        arity={
            "complete": "one",
            "complete_skipped": "zero",
            "complete_uncontested": "zero",
        },
    ),
    PhaseSpec(
        label="Phase 5",
        input_table="hand_starts",
        key_column="hand_start_id",
        attempts_table="hand_start_processing_attempts",
        output_table="hand_actions",
        arity={"complete": "one", "complete_skipped": "zero"},
    ),
)

# The natural id each stage table must not repeat. This is deliberately not
# `PhaseSpec.key_column`: that names the entity a phase consumes, which is the
# replace key, while this names the strongest uniqueness the table itself
# guarantees. Two of the five differ. `hand_starts` groups on `hand_setup_id`
# rather than its own `hand_start_id` because Phase 4 writes exactly one row per
# setup, making that the stronger invariant — and `hand_start_id` is derived as
# f"{hand_setup_id}_001" anyway.
DUPLICATE_KEYS: dict[str, str] = {
    "tournament_results": "video_id",
    "clip_manifest": "clip_id",
    "hand_setups": "hand_setup_id",
    "hand_starts": "hand_setup_id",
    "hand_actions": "hand_start_id",
}

# Statuses deliberately left unconstrained despite not being failures. Phase 3's
# `complete` spans zero rows — a clip may legitimately detect no hand setups —
# so no row count can be predicted from it. A contract test asserts that every
# status a writer can produce is either constrained by `arity`, in the `failed*`
# family, or listed here, so adding a status and forgetting the map fails loudly
# rather than silently disabling a check.
UNCONSTRAINED: dict[str, frozenset[str]] = {
    "Phase 3": frozenset({"complete"}),
}

# Rows counted for the report header.
STAGE_TABLES: tuple[str, ...] = tuple(DUPLICATE_KEYS)

# arity -> the SQL that matches a *violation* of it.
# Order matters only for readability: it fixes the order groups appear in both
# the generated predicate and the report's invariant block.
_VIOLATION: dict[str, str] = {
    "one": "!= 1",
    "at_least_one": "< 1",
    "zero": "!= 0",
}

# arity -> how the expectation reads in the report.
_EXPECTED: dict[str, str] = {
    "zero": "0 rows",
    "one": "1 row",
    "at_least_one": ">= 1 row",
}


@dataclass(frozen=True)
class Finding:
    check: str  # "unknown_video" | "orphan" | "duplicate" | "status_row"
    table: str
    entity_id: str
    video_id: str | None
    detail: str


@dataclass(frozen=True)
class IntegrityReport:
    videos_checked: int
    stage_rows: int
    findings: list[Finding]
    scope: tuple[str, ...] | None  # the --video-id list, or None for corpus-wide


def _video_filter(alias: str, *, scoped: bool) -> str:
    """The scope clause, or empty for a corpus-wide run.

    Callers pass `scoped=only_video_ids is not None`, never a truthiness test —
    an empty list scopes to nothing, not to everything.
    """
    return f"AND {alias}.video_id IN UNNEST(@only_video_ids)" if scoped else ""


def orphan_sql(spec: PhaseSpec, project: str, dataset: str, *, scoped: bool) -> str:
    """Stage rows in `spec.output_table` whose parent row no longer exists."""
    return f"""
        SELECT c.{spec.key_column} AS entity_id, c.video_id AS video_id
        FROM `{project}.{dataset}.{spec.output_table}` c
        LEFT JOIN `{project}.{dataset}.{spec.input_table}` par
          ON c.{spec.key_column} = par.{spec.key_column}
        WHERE par.{spec.key_column} IS NULL
          {_video_filter("c", scoped=scoped)}
    """


def duplicate_sql(table: str, key: str, project: str, dataset: str, *, scoped: bool) -> str:
    """Natural ids appearing on more than one row of `table`."""
    return f"""
        SELECT
          t.{key} AS entity_id,
          ANY_VALUE(t.video_id) AS video_id,
          COUNT(*) AS row_count
        FROM `{project}.{dataset}.{table}` t
        WHERE TRUE
          {_video_filter("t", scoped=scoped)}
        GROUP BY t.{key}
        HAVING COUNT(*) > 1
    """


def status_row_sql(spec: PhaseSpec, project: str, dataset: str, *, scoped: bool) -> str:
    """Entities whose latest attempt status contradicts their output row count.

    Per entity, never a count comparison: an aggregate delta of 3 at 150 videos
    is a research project, a named id is actionable.

    Statuses are interpolated as literals rather than parameters because they
    come from `PHASES`, a module constant with no external input.

    Raises ValueError if `spec.arity` is empty — a phase with no invariant has no
    query, and callers must skip it rather than run a predicate-less scan.
    """
    if not spec.arity:
        raise ValueError(f"{spec.label} has no status/row invariant")

    clauses = []
    for arity, violation in _VIOLATION.items():
        statuses = sorted(s for s, a in spec.arity.items() if a == arity)
        if not statuses:
            continue
        status_list = ", ".join(f"'{s}'" for s in statuses)
        clauses.append(
            f"(a.latest_status IN ({status_list}) "
            f"AND COALESCE(o.row_count, 0) {violation})"
        )

    key = spec.key_column
    return f"""
        WITH attempt_state AS (
          SELECT
            {key},
            ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS latest_status
          FROM `{project}.{dataset}.{spec.attempts_table}`
          GROUP BY {key}
        ),
        output_counts AS (
          SELECT {key}, COUNT(*) AS row_count
          FROM `{project}.{dataset}.{spec.output_table}`
          GROUP BY {key}
        )
        SELECT
          i.{key} AS entity_id,
          i.video_id AS video_id,
          a.latest_status AS latest_status,
          COALESCE(o.row_count, 0) AS row_count
        FROM `{project}.{dataset}.{spec.input_table}` i
        LEFT JOIN attempt_state a ON i.{key} = a.{key}
        LEFT JOIN output_counts o ON i.{key} = o.{key}
        WHERE ({" OR ".join(clauses)})
          {_video_filter("i", scoped=scoped)}
    """


def known_video_sql(project: str, dataset: str) -> str:
    """Which of the requested video ids actually exist."""
    return f"""
        SELECT v.video_id AS video_id
        FROM `{project}.{dataset}.videos` v
        WHERE v.video_id IN UNNEST(@only_video_ids)
    """


def row_count_sql(project: str, dataset: str, *, scoped: bool) -> str:
    """One row per counted table, for the report header."""
    parts = [
        f"SELECT 'videos' AS table_name, COUNT(*) AS n "
        f"FROM `{project}.{dataset}.videos` t "
        f"WHERE TRUE {_video_filter('t', scoped=scoped)}"
    ]
    parts += [
        f"SELECT '{table}', COUNT(*) FROM `{project}.{dataset}.{table}` t "
        f"WHERE TRUE {_video_filter('t', scoped=scoped)}"
        for table in STAGE_TABLES
    ]
    return "\n        UNION ALL ".join(parts)


def run_integrity_checks(
    *,
    project: str,
    dataset: str,
    only_video_ids: list[str] | None = None,
    client: bigquery.Client | None = None,
) -> IntegrityReport:
    """Run every check and return the findings. Reads only; writes nothing.

    Production callers leave `only_video_ids` as None to audit the whole corpus.
    Integration tests pass uuid-scoped lists to constrain the blast radius per
    CLAUDE.md. An empty list scopes to nothing, which is why the scope test is
    `is not None` throughout and never truthiness.
    """
    if client is None:
        client = bigquery.Client(project=project)

    scoped = only_video_ids is not None

    def run(sql: str) -> list:
        job_config = None
        if scoped:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids)
                ]
            )
        return list(client.query(sql, job_config=job_config).result())

    findings: list[Finding] = []

    # Unknown ids first. A typo would otherwise look like a clean result, which
    # is the worst possible failure for an audit tool.
    if scoped:
        known = {row.video_id for row in run(known_video_sql(project, dataset))}
        for video_id in only_video_ids:
            if video_id not in known:
                findings.append(
                    Finding(
                        check="unknown_video",
                        table="videos",
                        entity_id=video_id,
                        video_id=video_id,
                        detail="no such video",
                    )
                )

    counts = {row.table_name: row.n for row in run(row_count_sql(project, dataset, scoped=scoped))}
    videos_checked = counts.get("videos", 0)
    stage_rows = sum(counts.get(table, 0) for table in STAGE_TABLES)

    for spec in PHASES:
        # A phase whose input is `videos` has no parent among the stage tables;
        # `videos` is a fact table and Phase 1's territory, deliberately out of
        # scope for this tool.
        if spec.input_table == "videos":
            continue
        for row in run(orphan_sql(spec, project, dataset, scoped=scoped)):
            findings.append(
                Finding(
                    check="orphan",
                    table=spec.output_table,
                    entity_id=row.entity_id,
                    video_id=row.video_id,
                    detail=f"no {spec.input_table} row for {spec.key_column}",
                )
            )

    for table, key in DUPLICATE_KEYS.items():
        for row in run(duplicate_sql(table, key, project, dataset, scoped=scoped)):
            findings.append(
                Finding(
                    check="duplicate",
                    table=table,
                    entity_id=row.entity_id,
                    video_id=row.video_id,
                    detail=f"{row.row_count} rows share {key}",
                )
            )

    for spec in PHASES:
        if not spec.arity:
            continue
        for row in run(status_row_sql(spec, project, dataset, scoped=scoped)):
            expected = _EXPECTED[spec.arity[row.latest_status]]
            findings.append(
                Finding(
                    check="status_row",
                    table=spec.output_table,
                    entity_id=row.entity_id,
                    video_id=row.video_id,
                    detail=f"{row.latest_status} → expected {expected}, found {row.row_count}",
                )
            )

    return IntegrityReport(
        videos_checked=videos_checked,
        stage_rows=stage_rows,
        findings=findings,
        scope=tuple(only_video_ids) if scoped else None,
    )


def _invariant_block() -> list[str]:
    """The per-phase status/row expectations, generated from PHASES.

    Printed on every run, clean or not. Arity differs by phase, so the report
    names the invariant per phase rather than implying one rule — and Phase 3's
    absence reads as a property of the phase rather than an oversight.
    """
    lines = ["  status/row invariants, by phase:"]
    for spec in PHASES:
        if not spec.arity:
            lines.append(
                f"    {spec.output_table:<20}none — a clip may legitimately complete with zero"
            )
            lines.append(f"    {'':<20}hand setups, so row count cannot be predicted")
            continue
        groups = []
        for arity in _VIOLATION:
            statuses = sorted(s for s, a in spec.arity.items() if a == arity)
            if statuses:
                groups.append(f"{', '.join(statuses)} → {_EXPECTED[arity]}")
        lines.append(f"    {spec.output_table:<20}{'; '.join(groups)}")
    lines.append("")
    lines.append("  failure statuses are unconstrained in every phase: a stage row may")
    lines.append("  legitimately coexist with failed_transient or failed_parked.")
    return lines


def _summary_lines(findings: list[Finding]) -> list[str]:
    orphans = [f for f in findings if f.check == "orphan"]
    duplicates = [f for f in findings if f.check == "duplicate"]
    status_rows = [f for f in findings if f.check == "status_row"]
    unknown = [f for f in findings if f.check == "unknown_video"]

    def count(items: list[Finding]) -> str:
        videos = {f.video_id for f in items if f.video_id is not None}
        suffix = f"   ({len(videos)} video{'s' if len(videos) != 1 else ''})" if items else ""
        return f"{len(items):>4}{suffix}"

    lines = []
    if unknown:
        lines.append(f"  {'unknown video ids':<28}{count(unknown)}")
    if orphans:
        for table in STAGE_TABLES:
            rows = [f for f in orphans if f.table == table]
            if rows:
                lines.append(f"  {'orphaned ' + table:<28}{count(rows)}")
    else:
        lines.append(f"  {'orphaned rows':<28}{0:>4}")
    lines.append(f"  {'duplicate natural ids':<28}{count(duplicates)}")
    lines.append(f"  {'status/row mismatches':<28}{count(status_rows)}")
    return lines


_CHECK_HEADINGS = {
    "unknown_video": "unknown video ids",
    "orphan": "orphaned rows",
    "duplicate": "duplicate natural ids",
    "status_row": "status/row mismatches",
}


def _detail_lines(findings: list[Finding]) -> list[str]:
    lines = []
    for check, heading in _CHECK_HEADINGS.items():
        rows = [f for f in findings if f.check == check]
        if not rows:
            continue
        lines.append("")
        lines.append(f"  {heading} ({len(rows)})")
        for f in sorted(rows, key=lambda f: (f.table, f.entity_id)):
            lines.append(f"    {f.table:<20}{f.entity_id:<26}{f.detail}")
    return lines


def format_report(report: IntegrityReport) -> str:
    scope = ""
    if report.scope is not None:
        scope = "".join(f" --video-id {v}" for v in report.scope)
    plural = "" if report.videos_checked == 1 else "s"
    lines = [
        f"check-integrity{scope} — {report.videos_checked} video{plural}, "
        f"{report.stage_rows} stage rows",
        "",
    ]
    lines += _summary_lines(report.findings)
    lines.append("")

    if not report.findings:
        lines.append("  clean.")
    elif len(report.findings) <= DETAIL_THRESHOLD:
        lines += _detail_lines(report.findings)[1:]
    else:
        affected = sorted({f.video_id for f in report.findings if f.video_id is not None})
        lines.append(f"  affected: {', '.join(affected)}")
        lines.append(
            f"  {len(report.findings)} findings — run with --video-id for per-entity detail"
        )

    lines.append("")
    lines += _invariant_block()
    return "\n".join(lines)
