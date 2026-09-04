# The sanctioned reprocessing path, behind `tt mark-pending`.
#
# Deleting a stage table's rows does not cause a phase to re-run. Every pending
# query keys on latest attempt status, and CLAUDE.md rejects an
# output-existence guard because several outcomes are legitimately terminal with
# zero rows. So an entity whose latest status is `complete` is never re-selected
# whatever happened to its output; making it eligible again means appending a
# retryable attempt row.
#
# The second half is the part that is easy to get wrong. Re-running an upstream
# phase changes what downstream rows describe. Fewer setups on re-detection and
# the ids disappear, orphaning downstream rows whose `complete` attempts are
# never re-triggered. The same count at shifted moments and — because
# `hand_setup_id` is positional, `{clip_id}_{NNN}` — an id comes to describe a
# different hand, with no orphan, reconciling counts, a passing
# `tt check-integrity`, and silently wrong data. Deleting downstream rows as
# part of marking prevents both.
#
# COST: as in `integrity.py`, no `SELECT *` anywhere. BigQuery is columnar and
# the JSON blobs are nearly all the data; selecting them would turn a near-free
# query into a full-table scan at corpus scale.

import uuid
from dataclasses import dataclass, fields

from google.cloud import bigquery

from ._generated.clip_materialization_attempts_row import ClipMaterializationAttemptsRow
from ._generated.clip_processing_attempts_row import ClipProcessingAttemptsRow
from ._generated.hand_setup_processing_attempts_row import HandSetupProcessingAttemptsRow
from ._generated.hand_start_processing_attempts_row import HandStartProcessingAttemptsRow
from ._generated.tournament_results_processing_attempts_row import (
    TournamentResultsProcessingAttemptsRow,
)
from .clip_materialization_attempts_writer import write_clip_materialization_attempt_rows
from .clip_processing_attempts_writer import write_clip_processing_attempt_rows
from .hand_setup_processing_attempts_writer import write_hand_setup_processing_attempt_rows
from .hand_start_processing_attempts_writer import write_hand_start_processing_attempt_rows
from .integrity import PHASES
from .tournament_results_processing_attempts_writer import (
    write_tournament_results_processing_attempt_rows,
)

# Entities listed individually up to this many; above it the report collapses to
# a status histogram. Lower than `integrity.DETAIL_THRESHOLD` because this list
# is something an operator reads before committing to spend, not an audit result
# they scan afterwards.
DETAIL_THRESHOLD = 10

# A stage is named for what gets rebuilt — its output table — because that is how
# an operator thinks ("rebuild the hand starts"). Naming stages by input would
# make `--stage videos` ambiguous between payout extraction and materialization.
#
# The value is the stages downstream of the key, parent-first. This is a DAG and
# NOT the `PHASES` tuple order:
#
#   tournament_results --+
#                        +--> hand_setups --> hand_starts --> hand_actions
#   clip_manifest -------+
#
# Two stages feed `hand_setups` and neither feeds the other — materialization is
# arithmetic on `duration_seconds`, so re-reading the payout panel cannot change
# a clip window. Deriving this positionally from `PHASES` would make
# `--stage tournament_results` delete `clip_manifest`, which is not merely
# wasteful: it destroys clip ids and briefly orphans `hand_setups`.
#
# `videos` is deliberately absent. Re-running Phase 1 is re-acquisition, not
# reprocessing — it re-downloads from YouTube and may get a different encode —
# and Phase 1's status vocabulary predates the common one.
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "tournament_results": ("hand_setups", "hand_starts", "hand_actions"),
    "clip_manifest": ("hand_setups", "hand_starts", "hand_actions"),
    "hand_setups": ("hand_starts", "hand_actions"),
    "hand_starts": ("hand_actions",),
    "hand_actions": (),
}

STAGES: tuple[str, ...] = tuple(DOWNSTREAM)

# Reuses `integrity.PHASES` rather than restating the mapping. Anchoring each
# phase on its input table is what makes the stage-to-attempts off-by-one
# disappear in code: `--id` takes `spec.key_column`, the entity universe is
# `spec.input_table`, and the marks go to `spec.attempts_table` — which is named
# for the entity the phase *consumes*, so `hand_setup_processing_attempts`
# belongs to the phase producing `hand_starts`.
_SPEC = {spec.output_table: spec for spec in PHASES}

# The only retryable status every phase shares. `blocked_upstream` exists only in
# Phase 2 and means something specific; do not reuse it.
#
# Known consequence: `failed_transient` is inside the `failed%` family, so a mark
# appended after a `complete` leaves the entity at `consecutive_failures = 1` and
# it gets `max_attempts - 1` real attempts before parking. Pass
# `--max-attempts 4` on a rebuild run if the full three matter.
_MARK_STATUS = "failed_transient"

# Gemini calls one re-run makes per marked entity. The estimate exists to make
# someone hesitate, so order of magnitude is the useful part.
_CALLS_PER_ENTITY: dict[str, float] = {
    # One frame call; the fallback ladder costs nothing on the happy path.
    "tournament_results_processing_attempts": 1,
    # Arithmetic on duration_seconds. No LLM call at all.
    "clip_materialization_attempts": 0,
    # One clip scan plus one frame call per detected setup. ~5 setups per clip is
    # MPBLfM4mwfE's 65 over 12 clips.
    "clip_processing_attempts": 6,
    # Step-A scan plus the hole-card frame read.
    "hand_setup_processing_attempts": 2,
    # Measured: 204 calls over 60 hands (ARCHITECTURE, "What the corpus run
    # established").
    "hand_start_processing_attempts": 3.4,
}

# attempts table -> (row class, batched writer). Each writer validates against
# its own module's VALID_STATUSES.
_MARK_WRITERS = {
    "tournament_results_processing_attempts": (
        TournamentResultsProcessingAttemptsRow,
        write_tournament_results_processing_attempt_rows,
    ),
    "clip_materialization_attempts": (
        ClipMaterializationAttemptsRow,
        write_clip_materialization_attempt_rows,
    ),
    "clip_processing_attempts": (
        ClipProcessingAttemptsRow,
        write_clip_processing_attempt_rows,
    ),
    "hand_setup_processing_attempts": (
        HandSetupProcessingAttemptsRow,
        write_hand_setup_processing_attempt_rows,
    ),
    "hand_start_processing_attempts": (
        HandStartProcessingAttemptsRow,
        write_hand_start_processing_attempt_rows,
    ),
}


class MarkPendingError(Exception):
    pass


@dataclass(frozen=True)
class MarkPlan:
    """What a run would do, or did. Rendered by `format_plan`."""

    stage: str
    entity_table: str  # what --id refers to
    entity_noun: str  # the same thing in prose, for the report
    phase_label: str  # the phase consuming it, from integrity.PHASES
    entities: list[tuple[str, str | None]]  # (id, current_status)
    deletes: list[tuple[str, int]]  # (table, row_count), child before parent
    marks: list[tuple[str, int]]  # (attempts_table, row_count)
    missing_output: list[tuple[str, str]]  # (id, status) with zero output rows
    estimated_calls: int


@dataclass(frozen=True)
class _Entity:
    entity_id: str
    latest_status: str | None
    output_rows: int


@dataclass(frozen=True)
class _Resolution:
    """Everything the queries returned.

    `build_plan` is pure given one of these, so `--dry-run` and the real run
    cannot diverge.
    """

    video_id: str
    entities: list[_Entity]
    mark_ids: list[tuple[str, list[str]]]  # (attempts_table, ids), in mark order
    delete_counts: list[tuple[str, int]]  # (stage table, rows), child first
    key_column: str  # the column deletes and downstream lookups narrow on
    scope_ids: list[str] | None  # None means "the whole video", see resolve()


# --- query builders (pure) ---


def entity_sql(spec, project: str, dataset: str, *, by_id: bool, by_status: bool) -> str:
    """The entities `--stage` marks, with their latest status and output count.

    Reuses the codebase's latest-status idiom. `row_count` is what distinguishes
    "zero rows by design" from "rows missing unexpectedly" in the report; it is
    reported, never acted on — several outcomes are legitimately terminal with
    zero rows, so nothing here may infer pending-ness from output.
    """
    key = spec.key_column
    id_filter = f"AND i.{key} IN UNNEST(@only_ids)" if by_id else ""
    status_filter = "AND a.latest_status IN UNNEST(@only_statuses)" if by_status else ""
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
          a.latest_status AS latest_status,
          COALESCE(o.row_count, 0) AS row_count
        FROM `{project}.{dataset}.{spec.input_table}` i
        LEFT JOIN attempt_state a ON i.{key} = a.{key}
        LEFT JOIN output_counts o ON i.{key} = o.{key}
        WHERE i.video_id = @video_id
          {id_filter}
          {status_filter}
        ORDER BY i.{key}
    """


def mark_ids_sql(
    input_table: str,
    select_column: str,
    key_column: str,
    project: str,
    dataset: str,
    *,
    scoped: bool,
) -> str:
    """The entities of one downstream phase, narrowed by the named stage's key.

    Every downstream input table carries the named stage's `key_column` —
    `hand_setups`, `hand_starts` and `hand_actions` all carry `video_id` and
    `clip_id`, and the latter two also carry `hand_setup_id` — so one predicate
    shape covers every stage.
    """
    scope_filter = f"AND t.{key_column} IN UNNEST(@scope_ids)" if scoped else ""
    return f"""
        SELECT t.{select_column} AS entity_id
        FROM `{project}.{dataset}.{input_table}` t
        WHERE t.video_id = @video_id
          {scope_filter}
        ORDER BY t.{select_column}
    """


def _delete_target(
    table: str, key_column: str, project: str, dataset: str, *, scoped: bool
) -> str:
    """The table and predicate the count and the DELETE share, so they cannot drift."""
    scope_filter = f"AND {key_column} IN UNNEST(@scope_ids)" if scoped else ""
    return (
        f"`{project}.{dataset}.{table}`\n"
        f"        WHERE video_id = @video_id\n"
        f"          {scope_filter}"
    )


def delete_count_sql(
    table: str, key_column: str, project: str, dataset: str, *, scoped: bool
) -> str:
    target = _delete_target(table, key_column, project, dataset, scoped=scoped)
    return f"SELECT COUNT(*) AS n FROM {target}"


def delete_sql(
    table: str, key_column: str, project: str, dataset: str, *, scoped: bool
) -> str:
    target = _delete_target(table, key_column, project, dataset, scoped=scoped)
    return f"DELETE FROM {target}"


# --- resolution (all reads) ---


def resolve(
    *,
    stage: str,
    video_id: str,
    project: str,
    dataset: str,
    only_ids: list[str] | None = None,
    only_statuses: list[str] | None = None,
    client: bigquery.Client | None = None,
) -> _Resolution:
    """Read everything the plan needs. Writes nothing.

    Raises MarkPendingError if any `--id` does not name an entity of this video.
    A typo in one of five ids fails the whole command rather than silently
    marking four, and an id belonging to another video is an error rather than a
    silent widening of scope — the query is scoped to `video_id`, so such an id
    simply does not come back.
    """
    if stage not in DOWNSTREAM:
        raise MarkPendingError(f"Unknown stage {stage!r}. Must be one of: {list(STAGES)}")
    if client is None:
        client = bigquery.Client(project=project)

    spec = _SPEC[stage]

    def run(sql: str, params: list) -> list:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return list(client.query(sql, job_config=job_config).result())

    video_param = bigquery.ScalarQueryParameter("video_id", "STRING", video_id)

    params = [video_param]
    if only_ids is not None:
        params.append(bigquery.ArrayQueryParameter("only_ids", "STRING", only_ids))
    if only_statuses is not None:
        params.append(
            bigquery.ArrayQueryParameter("only_statuses", "STRING", only_statuses)
        )
    rows = run(
        entity_sql(
            spec,
            project,
            dataset,
            by_id=only_ids is not None,
            by_status=only_statuses is not None,
        ),
        params,
    )
    entities = [_Entity(r.entity_id, r.latest_status, r.row_count) for r in rows]

    if only_ids is not None:
        found = {e.entity_id for e in entities}
        missing = [i for i in only_ids if i not in found]
        if missing:
            raise MarkPendingError(
                f"No {spec.input_table} row for video {video_id} with "
                f"{spec.key_column} in {missing}. Nothing was written."
            )

    # Deletes and downstream lookups narrow by the resolved ids only when the
    # entity set is a strict subset. Unnarrowed, they scope by video_id alone,
    # which additionally sweeps orphaned downstream rows whose parent id no
    # longer exists — those can never be reached by an id list, since an orphan's
    # parent is by definition not in the entity set.
    narrowed = only_ids is not None or only_statuses is not None
    scope_ids = [e.entity_id for e in entities] if narrowed else None
    scope_params = [video_param]
    if scope_ids is not None:
        scope_params.append(bigquery.ArrayQueryParameter("scope_ids", "STRING", scope_ids))

    # Child before parent: a delete that fails partway must not have already
    # removed a parent whose children still reference it.
    delete_counts = []
    for table in reversed(DOWNSTREAM[stage]):
        result = run(
            delete_count_sql(
                table, spec.key_column, project, dataset, scoped=scope_ids is not None
            ),
            scope_params,
        )
        delete_counts.append((table, result[0].n))

    mark_ids: list[tuple[str, list[str]]] = [
        (spec.attempts_table, [e.entity_id for e in entities])
    ]
    for downstream_stage in DOWNSTREAM[stage]:
        downstream_spec = _SPEC[downstream_stage]
        result = run(
            mark_ids_sql(
                downstream_spec.input_table,
                downstream_spec.key_column,
                spec.key_column,
                project,
                dataset,
                scoped=scope_ids is not None,
            ),
            scope_params,
        )
        mark_ids.append((downstream_spec.attempts_table, [r.entity_id for r in result]))

    return _Resolution(
        video_id=video_id,
        entities=entities,
        mark_ids=mark_ids,
        delete_counts=delete_counts,
        key_column=spec.key_column,
        scope_ids=scope_ids,
    )


# --- plan (pure) ---


def _entity_noun(key_column: str) -> str:
    """The prose name of the entity a phase consumes: `hand_setup_id` -> "hand setup".

    Derived from `key_column` rather than mapped per stage so a new phase cannot
    acquire a stage entry without one.
    """
    return key_column.removesuffix("_id").replace("_", " ")


def build_plan(stage: str, resolution: _Resolution) -> MarkPlan:
    """Pure given query results, so `--dry-run` and the real run share it."""
    spec = _SPEC[stage]
    marks = [(table, len(ids)) for table, ids in resolution.mark_ids]
    return MarkPlan(
        stage=stage,
        entity_table=spec.input_table,
        entity_noun=_entity_noun(spec.key_column),
        phase_label=spec.label,
        entities=[(e.entity_id, e.latest_status) for e in resolution.entities],
        deletes=list(resolution.delete_counts),
        marks=marks,
        # A never-attempted entity with no rows is unprocessed, not noteworthy.
        missing_output=[
            (e.entity_id, e.latest_status)
            for e in resolution.entities
            if e.output_rows == 0 and e.latest_status is not None
        ],
        estimated_calls=round(sum(_CALLS_PER_ENTITY[table] * n for table, n in marks)),
    )


# --- execution (all writes) ---


def _key_column_for(row_class) -> str:
    """The id column of an attempts row — its one field that is not bookkeeping."""
    skip = {"attempt_id", "status", "status_message", "attempted_at"}
    for field in fields(row_class):
        if field.name not in skip:
            return field.name
    raise MarkPendingError(f"{row_class.__name__} has no id column")


def execute(
    plan: MarkPlan,
    resolution: _Resolution,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client,
) -> None:
    """Deletes first, child before parent, then the marks.

    If a delete fails partway the marks have not been written, so nothing is
    pending against a partially-cleared downstream — the operator re-runs and
    gets the same plan. Writing marks first and then failing a delete would leave
    entities pending with stale rows beneath them, which is the state this tool
    exists to prevent.
    """
    params = [bigquery.ScalarQueryParameter("video_id", "STRING", resolution.video_id)]
    if resolution.scope_ids is not None:
        params.append(
            bigquery.ArrayQueryParameter("scope_ids", "STRING", resolution.scope_ids)
        )

    for table, _count in plan.deletes:
        # Unconditional. The count was read a moment ago and is for the report;
        # nothing branches on it.
        sql = delete_sql(
            table,
            resolution.key_column,
            project,
            dataset,
            scoped=resolution.scope_ids is not None,
        )
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        client.query(sql, job_config=job_config).result()

    # The one trace distinguishing a synthetic mark from a real failure. Keep it
    # greppable: an audit reader must not mistake a deliberate reprocess for a
    # rate-limit incident.
    message = f"mark-pending: rebuilding {plan.stage}"
    for attempts_table, ids in resolution.mark_ids:
        row_class, write = _MARK_WRITERS[attempts_table]
        key_column = _key_column_for(row_class)
        has_attempt_id = any(f.name == "attempt_id" for f in fields(row_class))
        rows = []
        for entity_id in ids:
            kwargs = {
                key_column: entity_id,
                "status": _MARK_STATUS,
                "status_message": message,
            }
            if has_attempt_id:
                kwargs["attempt_id"] = uuid.uuid4().hex
            rows.append(row_class(**kwargs))
        write(rows, project=project, dataset=dataset, client=client)


def mark_pending(
    *,
    stage: str,
    video_id: str,
    project: str,
    dataset: str,
    only_ids: list[str] | None = None,
    only_statuses: list[str] | None = None,
    dry_run: bool = False,
    client: bigquery.Client | None = None,
) -> MarkPlan:
    """Make `stage` and everything downstream of it eligible for reprocessing.

    Deletes downstream rows only; the named stage's own rows are left alone,
    because replace semantics overwrite them on the next run and a DELETE up
    front would leave an entity with zero rows if processing then failed.

    Marks below the named stage are partly speculative: `--stage hand_setups`
    appends `hand_setup_processing_attempts` rows for ids that re-detection may
    not reproduce. This is harmless and expected — every pending query joins to
    the input table, so an attempt row for a vanished entity selects nothing.
    """
    if client is None:
        client = bigquery.Client(project=project)
    resolution = resolve(
        stage=stage,
        video_id=video_id,
        project=project,
        dataset=dataset,
        only_ids=only_ids,
        only_statuses=only_statuses,
        client=client,
    )
    plan = build_plan(stage, resolution)
    if not dry_run:
        execute(plan, resolution, project=project, dataset=dataset, client=client)
    return plan


# --- report ---


def _rows(n: int) -> str:
    return "row" if n == 1 else "rows"


def _calls(n: int) -> str:
    return "call" if n == 1 else "calls"


def format_plan(plan: MarkPlan, *, dry_run: bool) -> str:
    delete_heading = "WOULD DELETE" if dry_run else "DELETED"
    mark_heading = "WOULD MARK PENDING" if dry_run else "MARKED PENDING"

    count = len(plan.entities)
    noun = plan.entity_noun if count == 1 else f"{plan.entity_noun}s"
    # Naming the entity type is what makes passing a hand_setup_id where a
    # clip_id belongs fail visibly rather than silently marking nothing. It is
    # the entity that gets named and not its table: the input table is untouched
    # by a mark, so "Marking 13 clip_manifest entities" read as though
    # `clip_manifest` itself were about to be modified.
    head = (
        f"Rebuilding {plan.stage}. Marking {count} {noun} pending "
        f"({plan.phase_label} consumes {plan.entity_noun}s)"
    )

    lines = []
    if count == 0:
        return f"{head} — nothing matched."
    if count <= DETAIL_THRESHOLD:
        lines.append(f"{head}:")
        for entity_id, status in plan.entities:
            lines.append(f"  {entity_id:<28}{status or '(never attempted)'}")
    else:
        lines.append(f"{head}.")
        lines.append("")
        # The histogram earns its place: it shows, before you commit, that you
        # are about to un-park entities which will fail again and re-park.
        lines.append("  current status of marked entities:")
        histogram: dict[str, int] = {}
        for _entity_id, status in plan.entities:
            key = status or "(never attempted)"
            histogram[key] = histogram.get(key, 0) + 1
        for status, n in sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {status:<24}{n:>3}")

    if plan.missing_output:
        # Not an integrity assertion — `tt check-integrity` judges, this reports.
        # But "zero by design" and "rows missing unexpectedly" look identical
        # here, and the second is worth a closer look before spending.
        breakdown: dict[str, int] = {}
        for _entity_id, status in plan.missing_output:
            breakdown[status] = breakdown.get(status, 0) + 1
        detail = ", ".join(f"{n} {s}" for s, n in sorted(breakdown.items()))
        lines.append("")
        lines.append(
            f"  {len(plan.missing_output)} of these have no {plan.stage} row ({detail})."
        )
        lines.append(
            "  Zero rows is by design for the complete_* statuses; worth a look otherwise."
        )

    lines.append("")
    lines.append(delete_heading)
    if plan.deletes:
        for table, n in plan.deletes:
            lines.append(f"  {table:<40}{n:>4} {_rows(n)}")
    else:
        lines.append(f"  nothing — no stage is downstream of {plan.stage}")

    lines.append("")
    lines.append(mark_heading)
    for table, n in plan.marks:
        rate = _CALLS_PER_ENTITY[table]
        calls = round(rate * n)
        # Keyed on the rate, not the product: a phase that makes no LLM calls at
        # all reads differently from one with nothing to run this time.
        if not rate:
            suffix = "   (no LLM calls)"
        elif not calls:
            suffix = ""
        else:
            suffix = f"   (~{calls} Gemini {_calls(calls)})"
        lines.append(f"  {table:<40}{n:>4} {_rows(n)} appended{suffix}")

    lines.append("")
    lines.append(
        f"{plan.stage} rows are not deleted — replace semantics overwrite them on re-run."
    )
    lines.append("Nothing upstream is touched.")
    if len(plan.marks) > 1:
        lines.append(
            f"Marks below {plan.stage} may name ids re-detection will not reproduce; a"
        )
        lines.append("pending query joins to the stage table, so those select nothing.")

    if plan.estimated_calls:
        lines.append("")
        lines.append(
            f"Re-running will make roughly {plan.estimated_calls} Gemini "
            f"{_calls(plan.estimated_calls)}."
        )
    return "\n".join(lines)
