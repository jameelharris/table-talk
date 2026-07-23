# Phase 3 orchestrator: identify hand setups in clips, extract frames,
# call Gemini for player info, and land rows in hand_setups.
#
# process_clip() handles one clip atomically: either all hand_setups rows
# land (plus frames in GCS) or none do. Failures are recorded in
# clip_processing_attempts so that re-running the CLI retries them.
#
# process_pending_clips() coordinates across all pending clips,
# downloading each video once and processing its clips concurrently.

import asyncio
import os
import sys
import tempfile

from google.cloud import bigquery

from ._generated.clip_manifest_row import ClipManifestRow
from ._generated.clip_processing_attempts_row import ClipProcessingAttemptsRow
from ._generated.hand_setups_row import HandSetupsRow
from .clip_processing_attempts_writer import write_clip_processing_attempt_row
from .frame_extractor import extract_frame
from .frame_uploader import upload_frame
from .gemini_caller import GeminiPermanentError, call_gemini_for_clip, call_gemini_for_frame
from .hand_setups_writer import write_hand_setups
from .seat_enrichment import add_seat_numbers, normalize_heads_up
from .timestamp_utils import parse_timestamp
from .videos_downloader import DownloadPermanentError, download_video


def _find_pending_clips(
    project_id: str,
    dataset: str,
    only_clip_ids: list[str] | None = None,
    only_video_ids: list[str] | None = None,
    *,
    client: bigquery.Client | None = None,
) -> list[ClipManifestRow]:
    """Return clip_manifest rows pending processing.

    A clip is pending if it has never been attempted or its latest attempt
    status is 'failed_transient'. Clips with 'complete' or 'failed_permanent'
    are excluded.

    Production callers leave the scope params as None. Integration tests pass
    uuid-scoped lists to constrain the blast radius per CLAUDE.md.
    """
    if client is None:
        client = bigquery.Client(project=project_id)

    clip_filter = ""
    video_filter = ""
    params: list = []
    if only_clip_ids is not None:
        clip_filter = "AND m.clip_id IN UNNEST(@only_clip_ids)"
        params.append(bigquery.ArrayQueryParameter("only_clip_ids", "STRING", only_clip_ids))
    if only_video_ids is not None:
        video_filter = "AND m.video_id IN UNNEST(@only_video_ids)"
        params.append(bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids))

    query = f"""
        WITH latest_attempts AS (
          SELECT clip_id, status,
                 ROW_NUMBER() OVER (PARTITION BY clip_id ORDER BY attempted_at DESC) AS rn
          FROM `{project_id}.{dataset}.clip_processing_attempts`
        )
        SELECT m.*
        FROM `{project_id}.{dataset}.clip_manifest` m
        LEFT JOIN (SELECT * FROM latest_attempts WHERE rn = 1) a USING (clip_id)
        WHERE (a.status IS NULL OR a.status = 'failed_transient')
          {clip_filter}
          {video_filter}
        ORDER BY m.video_id, m.clip_start_time
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
    rows = list(client.query(query, job_config=job_config).result())
    return [
        ClipManifestRow(
            clip_id=row.clip_id,
            video_id=row.video_id,
            clip_start_time=row.clip_start_time,
            clip_end_time=row.clip_end_time,
        )
        for row in rows
    ]


async def process_clip(
    clip: ClipManifestRow,
    local_video_path: str,
    project_id: str,
    dataset: str,
    hand_setups_bucket: str,
    videos_bucket: str,
    identify_hand_prompt: str,
    extract_player_info_prompt: str,
) -> str:
    """Process one clip end-to-end. Returns the outcome status string.

    Never raises — all exceptions are caught, recorded as attempt rows,
    and translated into a return value so that process_pending_clips can
    continue to the next clip.
    """
    video_gcs_uri = f"gs://{videos_bucket}/{clip.video_id}.mp4"

    try:
        clip_result = await asyncio.to_thread(
            call_gemini_for_clip,
            identify_hand_prompt,
            video_gcs_uri,
            clip.clip_start_time,
            clip.clip_end_time,
            project_id,
        )
        hand_setups = clip_result.get("hand_setups", [])

        if not hand_setups:
            write_clip_processing_attempt_row(
                ClipProcessingAttemptsRow(
                    clip_id=clip.clip_id,
                    status="complete",
                    status_message="No hand setups detected",
                ),
                project=project_id,
                dataset=dataset,
            )
            return "complete"

        with tempfile.TemporaryDirectory() as frame_tmpdir:
            async def _process_one(ordinal: int, setup: dict):
                ts = parse_timestamp(setup["timestamp"])
                if not (clip.clip_start_time <= ts <= clip.clip_end_time):
                    raise GeminiPermanentError(
                        f"LLM returned timestamp {ts}s outside clip range "
                        f"[{clip.clip_start_time}, {clip.clip_end_time}] for clip {clip.clip_id} — "
                        f"treating as hallucination"
                    )
                temp_path = os.path.join(frame_tmpdir, f"frame_{ordinal:03d}.jpg")
                await asyncio.to_thread(extract_frame, local_video_path, ts, temp_path)
                with open(temp_path, "rb") as fh:
                    frame_bytes = fh.read()
                player_info = await asyncio.to_thread(
                    call_gemini_for_frame, extract_player_info_prompt, frame_bytes, project_id
                )
                return (ordinal, ts, player_info, temp_path)

            tasks = [_process_one(i + 1, setup) for i, setup in enumerate(hand_setups)]
            results_or_errors = await asyncio.gather(*tasks, return_exceptions=True)

            errors = [r for r in results_or_errors if isinstance(r, BaseException)]
            if errors:
                raise errors[0]

            results = sorted(results_or_errors, key=lambda r: r[0])

            rows = []
            for ordinal, ts, player_info, temp_path in results:
                hand_setup_id = f"{clip.clip_id}_{ordinal:03d}"
                frame_gcs_path = (
                    f"gs://{hand_setups_bucket}"
                    f"/{clip.video_id}/{clip.clip_id}/{hand_setup_id}.jpg"
                )
                await asyncio.to_thread(upload_frame, temp_path, frame_gcs_path, project_id)
                inner = player_info.get("hand_setup", {})
                hand_setup_state = {
                    "hand_setup_time_seconds": ts,
                    "total_seat_count": inner.get("total_seat_count"),
                    "pot_size_bb": inner.get("pot_size_bb"),
                    "players": inner.get("players", []),
                }
                add_seat_numbers(hand_setup_state)
                normalize_heads_up(hand_setup_state)
                rows.append(
                    HandSetupsRow(
                        hand_setup_id=hand_setup_id,
                        clip_id=clip.clip_id,
                        video_id=clip.video_id,
                        hand_setup_time_seconds=ts,
                        frame_gcs_path=frame_gcs_path,
                        hand_setup_state=hand_setup_state,
                    )
                )

            write_hand_setups(rows, project_id=project_id, dataset=dataset)
            write_clip_processing_attempt_row(
                ClipProcessingAttemptsRow(
                    clip_id=clip.clip_id,
                    status="complete",
                    status_message=f"{len(rows)} hand_setups detected",
                ),
                project=project_id,
                dataset=dataset,
            )

        return "complete"

    except GeminiPermanentError as exc:
        write_clip_processing_attempt_row(
            ClipProcessingAttemptsRow(
                clip_id=clip.clip_id,
                status="failed_permanent",
                status_message=str(exc)[:500],
            ),
            project=project_id,
            dataset=dataset,
        )
        return "failed_permanent"
    except Exception as exc:
        write_clip_processing_attempt_row(
            ClipProcessingAttemptsRow(
                clip_id=clip.clip_id,
                status="failed_transient",
                status_message=str(exc)[:500],
            ),
            project=project_id,
            dataset=dataset,
        )
        return "failed_transient"


async def process_pending_clips(
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_setups_bucket: str,
    identify_hand_prompt: str,
    extract_player_info_prompt: str,
    max_concurrent: int = 4,
    only_clip_ids: list[str] | None = None,
    only_video_ids: list[str] | None = None,
) -> dict[str, int]:
    """Process all pending clips. Returns summary stats.

    Videos are processed sequentially (one on disk at a time). Clips within
    each video are processed concurrently up to max_concurrent.
    """
    clips = _find_pending_clips(
        project_id, dataset, only_clip_ids=only_clip_ids, only_video_ids=only_video_ids
    )

    by_video: dict[str, list[ClipManifestRow]] = {}
    for clip in clips:
        by_video.setdefault(clip.video_id, []).append(clip)

    stats: dict[str, int] = {
        "clips_processed": 0,
        "clips_complete": 0,
        "clips_failed_transient": 0,
        "clips_failed_permanent": 0,
    }

    for video_id, video_clips in by_video.items():
        with tempfile.TemporaryDirectory() as tmpdir:
            local_video_path = os.path.join(tmpdir, f"{video_id}.mp4")

            try:
                await asyncio.to_thread(
                    download_video,
                    f"gs://{videos_bucket}/{video_id}.mp4",
                    local_video_path,
                    project_id,
                )
            except DownloadPermanentError as exc:
                print(f"Video {video_id} not found in GCS (permanent): {exc}", file=sys.stderr)
                for clip in video_clips:
                    write_clip_processing_attempt_row(
                        ClipProcessingAttemptsRow(
                            clip_id=clip.clip_id,
                            status="failed_permanent",
                            status_message=f"video_download_not_found: {str(exc)[:400]}",
                        ),
                        project=project_id,
                        dataset=dataset,
                    )
                    stats["clips_processed"] += 1
                    stats["clips_failed_permanent"] += 1
                continue
            except Exception as exc:
                print(f"Failed to download video {video_id}: {exc}", file=sys.stderr)
                for clip in video_clips:
                    write_clip_processing_attempt_row(
                        ClipProcessingAttemptsRow(
                            clip_id=clip.clip_id,
                            status="failed_transient",
                            status_message=f"video_download_failed: {str(exc)[:400]}",
                        ),
                        project=project_id,
                        dataset=dataset,
                    )
                    stats["clips_processed"] += 1
                    stats["clips_failed_transient"] += 1
                continue

            sem = asyncio.Semaphore(max_concurrent)

            async def _run_clip(c: ClipManifestRow) -> str:
                async with sem:
                    return await process_clip(
                        c,
                        local_video_path,
                        project_id,
                        dataset,
                        hand_setups_bucket,
                        videos_bucket,
                        identify_hand_prompt,
                        extract_player_info_prompt,
                    )

            clip_tasks = [_run_clip(c) for c in video_clips]
            outcomes = await asyncio.gather(*clip_tasks)

            for outcome in outcomes:
                stats["clips_processed"] += 1
                key = f"clips_{outcome}"
                if key in stats:
                    stats[key] += 1

    return stats
