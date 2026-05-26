from google.cloud import bigquery

from ._generated.clip_manifest_row import ClipManifestRow
from .clip_manifest_writer import write_clip_manifest_row

_CLIP_WINDOW_SECONDS = 240


class MaterializeError(Exception):
    pass


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

    ordinal = 1
    start = 0
    while start < duration_seconds:
        end = min(ordinal * _CLIP_WINDOW_SECONDS, duration_seconds)
        clip_id = f"{video_id}_{ordinal:03d}"
        write_clip_manifest_row(
            ClipManifestRow(
                clip_id=clip_id,
                video_id=video_id,
                clip_start_time=start,
                clip_end_time=end,
            ),
            project=project,
            dataset=dataset,
            client=bq_client,
        )
        start = end
        ordinal += 1


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

    pending_rows = list(
        bq_client.query(
            f"""
            SELECT v.video_id
            FROM `{project}.{dataset}.videos` v
            LEFT JOIN `{project}.{dataset}.clip_manifest` m ON m.video_id = v.video_id
            WHERE m.video_id IS NULL
            {scope_filter}
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=params) if params else None,
        )
    )

    succeeded = 0
    failed = 0
    for row in pending_rows:
        try:
            materialize_clips(row.video_id, project=project, dataset=dataset, bq_client=bq_client)
            succeeded += 1
        except Exception as exc:
            print(f"Failed to materialize clips for {row.video_id}: {exc}")
            failed += 1

    print(f"Materialized clips for {succeeded} videos, {failed} failed.")
