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
# skipped and the run continues; with --video-id the operator has asserted
# intent about one specific video, and an error is the honest answer.

from google.cloud import bigquery

from ._generated.clip_manifest_row import ClipManifestRow
from .clip_manifest_writer import write_clip_manifest_rows

_CLIP_WINDOW_SECONDS = 240


class MaterializeError(Exception):
    pass


def _payout_gate_reason(video_id: str, latest_status: str | None) -> str:
    """Explain why a video with no `tournament_results` row cannot be materialized.

    The absence alone is not actionable — the operator's next move differs by
    cause — so the latest attempt status is read from
    `tournament_results_processing_attempts` and reported alongside it. Phase 2
    writes no state row of its own precisely because the cause is already
    durably recorded one phase upstream; a Phase 2 row would record a
    consequence rather than a cause.
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


def _check_payout_gate(
    video_id: str,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
) -> None:
    """Raise MaterializeError unless this video has a `tournament_results` row.

    Re-queries rather than trusting a caller. On the batch path the pending
    query has already established the answer, so this costs one extra query per
    materialized video — accepted, because it is what makes the --video-id path
    safe on its own terms rather than by convention.
    """
    row = list(
        bq_client.query(
            f"""
            SELECT
              (SELECT COUNT(1) FROM `{project}.{dataset}.tournament_results`
               WHERE video_id = @video_id) AS payout_rows,
              (SELECT status FROM `{project}.{dataset}.tournament_results_processing_attempts`
               WHERE video_id = @video_id
               ORDER BY attempted_at DESC LIMIT 1) AS latest_status
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        )
    )[0]
    if not row.payout_rows:
        raise MaterializeError(
            f"video_id {video_id!r}: {_payout_gate_reason(video_id, row.latest_status)}"
        )


def materialize_clips(
    video_id: str,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
) -> None:
    video_rows = list(
        bq_client.query(
            f"SELECT duration_seconds FROM `{project}.{dataset}.videos` WHERE video_id = @video_id LIMIT 1",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        )
    )
    if not video_rows:
        raise MaterializeError(f"video_id {video_id!r} not found in videos table")

    duration_seconds = video_rows[0].duration_seconds
    if duration_seconds <= 0:
        raise MaterializeError(
            f"video_id {video_id!r} has invalid duration_seconds={duration_seconds}"
        )

    existing = list(
        bq_client.query(
            f"SELECT clip_id FROM `{project}.{dataset}.clip_manifest` WHERE video_id = @video_id LIMIT 1",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        )
    )
    if existing:
        return

    # After the idempotence return, not before: a video materialized earlier —
    # including one materialized before this gate existed — is already in the
    # corpus, and re-running must not start erroring on it. Nothing new is
    # admitted on that path.
    _check_payout_gate(video_id, project=project, dataset=dataset, bq_client=bq_client)

    rows = []
    ordinal = 1
    start = 0
    while start < duration_seconds:
        end = min(ordinal * _CLIP_WINDOW_SECONDS, duration_seconds)
        clip_id = f"{video_id}_{ordinal:03d}"
        rows.append(
            ClipManifestRow(
                clip_id=clip_id,
                video_id=video_id,
                clip_start_time=start,
                clip_end_time=end,
            )
        )
        start = end
        ordinal += 1

    write_clip_manifest_rows(rows, project=project, dataset=dataset, client=bq_client)


def materialize_clips_for_pending_videos(
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
    only_video_ids: list[str] | None = None,
) -> None:
    """Materialize clips for all pending videos (or a scoped subset).

    Production callers leave `only_video_ids` as None to scan all pending videos.
    Integration tests pass `only_video_ids` to constrain the function's blast radius
    to test-owned data, per the integration test scoping convention in CLAUDE.md.
    """
    scope_filter = ""
    params = []
    if only_video_ids is not None:
        scope_filter = "AND v.video_id IN UNNEST(@only_video_ids)"
        params = [bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids)]

    # Driven off `videos` and LEFT JOINed to `tournament_results`, not the
    # reverse: a payout-less video must be visible-and-skipped, not invisible.
    # Driving off the stage table would drop it from the result set entirely,
    # and a video that never appears cannot be reported — which is the whole
    # point of the gate, since a silent no-op looks like success.
    pending_rows = list(
        bq_client.query(
            f"""
            SELECT
              v.video_id,
              tr.video_id IS NOT NULL AS has_payouts,
              a.latest_status
            FROM `{project}.{dataset}.videos` v
            LEFT JOIN `{project}.{dataset}.clip_manifest` m ON m.video_id = v.video_id
            LEFT JOIN `{project}.{dataset}.tournament_results` tr ON tr.video_id = v.video_id
            LEFT JOIN (
              SELECT
                video_id,
                ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS latest_status
              FROM `{project}.{dataset}.tournament_results_processing_attempts`
              GROUP BY video_id
            ) a ON a.video_id = v.video_id
            WHERE m.video_id IS NULL
            {scope_filter}
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=params) if params else None,
        )
    )

    succeeded = 0
    failed = 0
    skipped = 0
    for row in pending_rows:
        # Tallied and worded separately from a failure. A video awaiting payout
        # extraction is a normal outcome of a correctly ordered pipeline, not an
        # error, and reporting it as one would train operators to ignore it.
        if not row.has_payouts:
            print(f"Skipped {row.video_id}: {_payout_gate_reason(row.video_id, row.latest_status)}")
            skipped += 1
            continue
        try:
            materialize_clips(row.video_id, project=project, dataset=dataset, bq_client=bq_client)
            succeeded += 1
        except Exception as exc:
            print(f"Failed to materialize clips for {row.video_id}: {exc}")
            failed += 1

    print(
        f"Materialized clips for {succeeded} videos, {failed} failed, "
        f"{skipped} skipped (no payout data)."
    )
