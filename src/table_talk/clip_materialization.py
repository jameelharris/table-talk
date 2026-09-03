# Phase 2 orchestrator: compute 240-second clip windows from a video's
# duration and write the manifest to BQ.
#
# Materialization is gated on payout extraction. A hand without payout context
# cannot participate in ICM analysis, and a corpus mixing hands that have it
# with hands that do not cannot be compared — so payout data is a precondition
# for a hand entering the corpus at all, not a filter applied at query time.
# That places the gate here rather than in Phase 3: if payouts define
# admissibility, clip_manifest should never hold a row that ought not be
# processed, and Phase 3's pending set is then correct by construction.
#
# Both entry points enforce it, because admissibility cannot depend on which
# code path the operator took. They differ in how they report: in the pending
# set a payout-less video is one of many not yet ready, so it is named and
# recorded as 'blocked_upstream' and the run continues; with --video-id the
# operator has asserted intent about one specific video, so the same row is
# written and then an error is raised.
#
# Every path through this module writes exactly one attempt row to
# clip_materialization_attempts. Pending-ness is derived from that table's
# latest status, not from the presence of clip_manifest rows, so an outcome
# that records nothing leaves the retry cap unable to advance.

import uuid
from dataclasses import dataclass

from google.cloud import bigquery

from ._generated.clip_manifest_row import ClipManifestRow
from ._generated.clip_materialization_attempts_row import ClipMaterializationAttemptsRow
from .clip_manifest_writer import write_clip_manifest_rows
from .clip_materialization_attempts_writer import write_clip_materialization_attempt_row

_CLIP_WINDOW_SECONDS = 240

# Retryable statuses. 'blocked_upstream' is retryable but is deliberately not
# a 'failed%' status: the consecutive-failure counter below keys on that
# prefix, so a video waiting on payout extraction resets the counter rather
# than advancing it toward the cap for a condition that is not its fault.
_RETRYABLE_STATUSES = ("failed_transient", "blocked_upstream")


class MaterializeError(Exception):
    pass


@dataclass(frozen=True)
class PendingVideo:
    video_id: str
    duration_seconds: int
    has_payouts: bool
    payout_latest_status: str | None
    consecutive_failures: int


def _payout_gate_reason(video_id: str, latest_status: str | None) -> str:
    """Explain why a video with no `tournament_results` row cannot be materialized.

    The absence alone is not actionable — the operator's next move differs by
    cause — so the latest attempt status is read from
    `tournament_results_processing_attempts` and reported alongside it. The
    Phase 2 row this reason is attached to records the consequence
    ('blocked_upstream'); the cause stays durably recorded one phase upstream.
    """
    if latest_status is None:
        return (
            "no tournament_results row and no extraction attempt — run "
            f"`tt extract-payouts --video-id {video_id}`"
        )
    if latest_status == "failed_transient":
        return (
            "no tournament_results row; the last extraction failed transiently — "
            f"re-run `tt extract-payouts --video-id {video_id}`"
        )
    if latest_status == "complete":
        # Should not occur: 'complete' is written only after the row lands. One
        # branch, because without it this case would print a misleading message.
        return (
            "no tournament_results row, but the latest extraction attempt reported "
            "'complete' — anomalous; inspect tournament_results_processing_attempts"
        )
    return (
        f"no tournament_results row; extraction terminated {latest_status!r} — the "
        "video may not be usable; see tournament_results_processing_attempts"
    )


def _transient_status(consecutive_failures: int, max_attempts: int) -> str:
    """Which status to write for a transient failure. The current failure is not
    yet counted in consecutive_failures, hence the +1."""
    return "failed_parked" if consecutive_failures + 1 >= max_attempts else "failed_transient"


def _video_state_sql(project: str, dataset: str, where_clause: str) -> str:
    """The SELECT both entry points read their PendingVideo from.

    They differ only in WHERE: the batch path filters on the pending predicate,
    while --video-id must work whatever the latest status is, since deliberate
    reprocessing is the point. `consecutive_failures` counts only failures since
    the last non-failure — a failure can follow a success, so a lifetime count
    would park healthy videos early — and the 'failed%' prefix match is what
    keeps 'blocked_upstream' from advancing it.
    """
    return f"""
        WITH attempt_marks AS (
          SELECT
            video_id, status, attempted_at,
            MAX(IF(status NOT LIKE 'failed%', attempted_at, NULL)) OVER (
              PARTITION BY video_id
            ) AS last_non_failure_at
          FROM `{project}.{dataset}.clip_materialization_attempts`
        ),
        attempt_state AS (
          SELECT
            video_id,
            ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS latest_status,
            COUNTIF(
              status = 'failed_transient'
              AND (last_non_failure_at IS NULL OR attempted_at > last_non_failure_at)
            ) AS consecutive_failures
          FROM attempt_marks
          GROUP BY video_id
        ),
        payout_attempt_state AS (
          SELECT
            video_id,
            ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS payout_latest_status
          FROM `{project}.{dataset}.tournament_results_processing_attempts`
          GROUP BY video_id
        )
        SELECT
          v.video_id,
          v.duration_seconds,
          tr.video_id IS NOT NULL AS has_payouts,
          p.payout_latest_status,
          COALESCE(a.consecutive_failures, 0) AS consecutive_failures
        FROM `{project}.{dataset}.videos` v
        LEFT JOIN `{project}.{dataset}.tournament_results` tr ON tr.video_id = v.video_id
        LEFT JOIN attempt_state a ON a.video_id = v.video_id
        LEFT JOIN payout_attempt_state p ON p.video_id = v.video_id
        WHERE {where_clause}
        ORDER BY v.video_id
    """


def _to_pending_video(row) -> PendingVideo:
    return PendingVideo(
        video_id=row.video_id,
        duration_seconds=row.duration_seconds,
        has_payouts=row.has_payouts,
        payout_latest_status=row.payout_latest_status,
        consecutive_failures=row.consecutive_failures,
    )


def _find_pending_videos(
    project: str,
    dataset: str,
    only_video_ids: list[str] | None = None,
    *,
    client: bigquery.Client,
) -> list[PendingVideo]:
    """Return videos pending clip materialization.

    A video is pending if it has never been attempted or its latest attempt
    status is 'failed_transient' or 'blocked_upstream'. Videos with 'complete',
    'failed_permanent', or 'failed_parked' are excluded.

    Driven off `videos` and LEFT JOINed to `tournament_results`, not the
    reverse: a payout-less video must be visible-and-blocked, not invisible.
    Driving off the stage table would drop it from the result set entirely,
    and a video that never appears cannot be reported — which is the whole
    point of the gate, since a silent no-op looks like success.

    There is deliberately no guard against existing clip_manifest rows. Pending
    is a property of the attempts table alone; a video re-selected after a
    retryable outcome may well already have rows, and the writer's replace
    semantics are what make that safe.

    Production callers leave `only_video_ids` as None to scan all pending videos.
    Integration tests pass `only_video_ids` to constrain the function's blast radius
    to test-owned data, per the integration test scoping convention in CLAUDE.md.
    """
    scope_filter = ""
    params: list = []
    if only_video_ids is not None:
        scope_filter = "AND v.video_id IN UNNEST(@only_video_ids)"
        params.append(bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids))

    where_clause = (
        "(a.latest_status IS NULL OR a.latest_status IN "
        f"({', '.join(repr(s) for s in _RETRYABLE_STATUSES)}))\n          {scope_filter}"
    )
    query = _video_state_sql(project, dataset, where_clause)
    job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
    rows = list(client.query(query, job_config=job_config).result())
    return [_to_pending_video(row) for row in rows]


def _find_video(
    video_id: str,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
) -> PendingVideo | None:
    """Read one video's state regardless of its latest attempt status.

    `consecutive_failures` is read here, up front, rather than inside the
    failure handler: the handler runs precisely when BigQuery may be
    unreachable, and a read there would fail too, silently defeating the cap
    for every video an outage touches.
    """
    query = _video_state_sql(project, dataset, "v.video_id = @video_id")
    rows = list(
        bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        ).result()
    )
    return _to_pending_video(rows[0]) if rows else None


def _write_attempt(
    video_id: str,
    status: str,
    status_message: str | None,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
) -> None:
    write_clip_materialization_attempt_row(
        ClipMaterializationAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            video_id=video_id,
            status=status,
            status_message=status_message,
        ),
        project=project,
        dataset=dataset,
        client=bq_client,
    )


def _compute_clip_rows(video_id: str, duration_seconds: int) -> list[ClipManifestRow]:
    rows = []
    ordinal = 1
    start = 0
    while start < duration_seconds:
        end = min(ordinal * _CLIP_WINDOW_SECONDS, duration_seconds)
        rows.append(
            ClipManifestRow(
                clip_id=f"{video_id}_{ordinal:03d}",
                video_id=video_id,
                clip_start_time=start,
                clip_end_time=end,
            )
        )
        start = end
        ordinal += 1
    return rows


def _materialize_one(
    video: PendingVideo,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
    max_attempts: int,
) -> tuple[str, str | None]:
    """Materialize one video. Never raises.

    Writes exactly one attempt row on every path and returns
    (status, status_message). Both entry points go through here, which is what
    keeps the one-row-per-attempt property from depending on which one was used.
    """
    if not video.has_payouts:
        reason = _payout_gate_reason(video.video_id, video.payout_latest_status)
        _write_attempt(
            video.video_id, "blocked_upstream", reason,
            project=project, dataset=dataset, bq_client=bq_client,
        )
        return "blocked_upstream", reason

    if video.duration_seconds <= 0:
        message = (
            f"video_id {video.video_id!r} has invalid "
            f"duration_seconds={video.duration_seconds}"
        )
        _write_attempt(
            video.video_id, "failed_permanent", message,
            project=project, dataset=dataset, bq_client=bq_client,
        )
        return "failed_permanent", message

    try:
        write_clip_manifest_rows(
            _compute_clip_rows(video.video_id, video.duration_seconds),
            video_id=video.video_id,
            project=project,
            dataset=dataset,
            client=bq_client,
        )
    except Exception as exc:
        status = _transient_status(video.consecutive_failures, max_attempts)
        message = f"{status}: {exc}"
        _write_attempt(
            video.video_id, status, message,
            project=project, dataset=dataset, bq_client=bq_client,
        )
        return status, message

    _write_attempt(
        video.video_id, "complete", None,
        project=project, dataset=dataset, bq_client=bq_client,
    )
    return "complete", None


def materialize_clips(
    video_id: str,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
    max_attempts: int = 3,
) -> None:
    """Materialize clips for one video, whatever its latest attempt status.

    Raises MaterializeError on any non-'complete' outcome, after the attempt row
    for that outcome has been written — a --video-id run always leaves a trace.
    """
    video = _find_video(video_id, project=project, dataset=dataset, bq_client=bq_client)
    if video is None:
        # The one outcome _materialize_one cannot see: it takes a PendingVideo,
        # which a video absent from `videos` cannot produce.
        message = f"video_id {video_id!r} not found in videos table"
        _write_attempt(
            video_id, "failed_permanent", message,
            project=project, dataset=dataset, bq_client=bq_client,
        )
        raise MaterializeError(message)

    status, message = _materialize_one(
        video, project=project, dataset=dataset, bq_client=bq_client, max_attempts=max_attempts
    )
    if status != "complete":
        raise MaterializeError(f"video_id {video_id!r}: {message}")


def materialize_clips_for_pending_videos(
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
    only_video_ids: list[str] | None = None,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Materialize clips for all pending videos (or a scoped subset).

    Production callers leave `only_video_ids` as None to scan all pending videos.
    Integration tests pass `only_video_ids` to constrain the function's blast radius
    to test-owned data, per the integration test scoping convention in CLAUDE.md.
    """
    pending = _find_pending_videos(project, dataset, only_video_ids, client=bq_client)

    # Every status gets a key up front. A dict populated only as outcomes occur
    # invites the `if key in stats` guard that silently drops an unbucketed
    # status, and counts then stop summing to videos_processed with no error.
    stats = {
        "videos_processed": 0,
        "videos_complete": 0,
        "videos_blocked_upstream": 0,
        "videos_failed_transient": 0,
        "videos_failed_permanent": 0,
        "videos_failed_parked": 0,
    }

    for video in pending:
        stats["videos_processed"] += 1
        status, message = _materialize_one(
            video, project=project, dataset=dataset, bq_client=bq_client, max_attempts=max_attempts
        )
        stats[f"videos_{status}"] += 1
        if status == "blocked_upstream":
            # Worded as a block rather than a failure. A video awaiting payout
            # extraction is a normal outcome of a correctly ordered pipeline,
            # and reporting it as an error would train operators to ignore it.
            print(f"Blocked {video.video_id}: {message}")
        elif status != "complete":
            print(f"Failed to materialize clips for {video.video_id}: {message}")

    return stats
