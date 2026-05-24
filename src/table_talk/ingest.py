# Ingestion orchestrator. Reads a YAML manifest, reconciles against
# BigQuery to determine eligible URLs, and runs each one end-to-end
# through the four primitives (fetcher, uploader, writers).
#
# process_url() is the per-URL execution function; process_manifest()
# is the manifest-wide coordinator that loops over it.

import tempfile
import time
from enum import StrEnum
from pathlib import Path

from google.cloud import bigquery, storage

from .manifest import ManifestError, extract_video_id, load_manifest
from .video_ingestion_attempts_writer import AttemptRow, write_attempt_row
from .videos_fetcher import TerminalFetchError, TransientFetchError, fetch_video
from .videos_uploader import UploadError, upload_video
from .videos_writer import VideoRow, VideosWriteError, write_video_row


class Decision(StrEnum):
    INGEST = "ingest"
    SKIP_ALREADY_COMPLETE = "skip_already_complete"
    SKIP_TERMINAL_FAILURE = "skip_terminal_failure"


def reconcile_url(
    url: str,
    video_id: str,
    *,
    project: str,
    dataset: str,
    bq_client: bigquery.Client,
) -> Decision:
    videos_query = f"""
        SELECT video_id FROM `{project}.{dataset}.videos`
        WHERE video_id = @video_id
        LIMIT 1
    """
    rows = list(
        bq_client.query(
            videos_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        )
    )
    if rows:
        return Decision.SKIP_ALREADY_COMPLETE

    attempts_query = f"""
        SELECT status FROM `{project}.{dataset}.video_ingestion_attempts`
        WHERE source_url = @source_url
        ORDER BY attempted_at DESC
        LIMIT 1
    """
    rows = list(
        bq_client.query(
            attempts_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("source_url", "STRING", url)]
            ),
        )
    )
    if not rows:
        return Decision.INGEST

    status = rows[0].status
    if status == "failed_terminal":
        return Decision.SKIP_TERMINAL_FAILURE
    return Decision.INGEST


def process_url(
    url: str,
    *,
    project: str,
    dataset: str,
    bucket: str,
    bq_client: bigquery.Client,
    storage_client: storage.Client,
) -> None:
    print(f"Processing URL: {url}")
    start = time.monotonic()

    try:
        video_id = extract_video_id(url)
    except ManifestError as exc:
        write_attempt_row(
            AttemptRow(
                source_url=url,
                status="failed_terminal",
                status_message=f"manifest_error: {exc}",
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
            project=project,
            dataset=dataset,
            client=bq_client,
        )
        return

    decision = reconcile_url(url, video_id, project=project, dataset=dataset, bq_client=bq_client)
    if decision != Decision.INGEST:
        print(f"Skipping {url}: {decision.value}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        try:
            fetch_result = fetch_video(url, download_dir=tmpdir_path)
        except TerminalFetchError as exc:
            write_attempt_row(
                AttemptRow(
                    source_url=url,
                    status="failed_terminal",
                    status_message=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000),
                ),
                project=project,
                dataset=dataset,
                client=bq_client,
            )
            return
        except TransientFetchError as exc:
            write_attempt_row(
                AttemptRow(
                    source_url=url,
                    status="failed_transient_predownload",
                    status_message=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000),
                ),
                project=project,
                dataset=dataset,
                client=bq_client,
            )
            return

        try:
            upload_result = upload_video(
                fetch_result.local_path,
                fetch_result.video_id,
                bucket=bucket,
                client=storage_client,
            )
        except UploadError as exc:
            write_attempt_row(
                AttemptRow(
                    source_url=url,
                    status="failed_transient_predownload",
                    status_message=f"gcs_upload_failed: {exc}",
                    video_id=fetch_result.video_id,
                    duration_ms=int((time.monotonic() - start) * 1000),
                ),
                project=project,
                dataset=dataset,
                client=bq_client,
            )
            return

        try:
            write_video_row(
                VideoRow(
                    video_id=fetch_result.video_id,
                    source_url=url,
                    title=fetch_result.title,
                    duration_seconds=fetch_result.duration_seconds,
                    gcs_path=upload_result.gcs_uri,
                    file_size_bytes=upload_result.size_bytes,
                ),
                project=project,
                dataset=dataset,
                client=bq_client,
            )
        except VideosWriteError as exc:
            write_attempt_row(
                AttemptRow(
                    source_url=url,
                    status="failed_transient_postdownload",
                    status_message=f"bq_write_failed: {exc}",
                    video_id=fetch_result.video_id,
                    duration_ms=int((time.monotonic() - start) * 1000),
                ),
                project=project,
                dataset=dataset,
                client=bq_client,
            )
            return

        write_attempt_row(
            AttemptRow(
                source_url=url,
                status="complete",
                video_id=fetch_result.video_id,
                duration_ms=int((time.monotonic() - start) * 1000),
            ),
            project=project,
            dataset=dataset,
            client=bq_client,
        )


def process_manifest(
    manifest_path: Path,
    *,
    project: str,
    dataset: str,
    bucket: str,
    bq_client: bigquery.Client,
    storage_client: storage.Client,
) -> None:
    entries = load_manifest(manifest_path)
    for entry in entries:
        try:
            process_url(
                entry.source_url,
                project=project,
                dataset=dataset,
                bucket=bucket,
                bq_client=bq_client,
                storage_client=storage_client,
            )
        except Exception as exc:
            print(f"Unexpected error processing {entry.source_url}: {exc}")
    print(f"Processed {len(entries)} URLs.")
