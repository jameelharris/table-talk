# Payout extraction orchestrator: read the tournament results panel from one
# early frame of a video and land a single row in tournament_results carrying
# the prize ladder and the tournament's bounty format.
#
# Two things downstream needs and nothing else produces. ICM is a function of
# the prize structure, so without the ladder no ICM question is answerable. And
# Phase 3 selects a different extraction prompt for bounty events; video titles
# are not a reliable signal (oWKpfjfEM4c is a confirmed PKO whose title does not
# say so), and the results panel — which gains a Bounty column on knockout
# events — is the only reliable source. Both read off the same frame, so one
# Gemini call per video serves both.
#
# process_video() handles one video atomically: either the tournament_results
# row lands or none does. Outcomes are recorded in
# tournament_results_processing_attempts so re-running the CLI retries transient
# failures and skips completed/skipped/permanently-failed/parked ones.
#
# This phase must run before `tt process-clips`. That ordering is enforced by
# Phase 3, not here, and `tt ingest` deliberately does not chain into this
# command — chaining would entangle two independent failure modes and prevent
# re-running payout extraction alone after a prompt fix.

import asyncio
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass

from google.cloud import bigquery

from ._generated.tournament_results_processing_attempts_row import (
    TournamentResultsProcessingAttemptsRow,
)
from ._generated.tournament_results_row import TournamentResultsRow
from .frame_extractor import extract_frame
from .frame_uploader import upload_frame
from .gemini_caller import GeminiPermanentError, call_gemini_for_frame
from .tournament_results_processing_attempts_writer import (
    write_tournament_results_processing_attempt_row,
)
from .tournament_results_writer import write_tournament_results
from .videos_downloader import DownloadPermanentError, download_video

# The panel is populated from the start of the replay — screenshot evidence
# shows a full nine-rank ladder while the chat still reads "Replay resumed" — so
# an early frame is the cheapest place to read it. But an early frame can also
# land on a title card or a transition, hence the ladder. It costs nothing on
# the happy path: rung one succeeds and the rest are never extracted.
FRAME_FALLBACK_LADDER = (5, 30, 120)

# A final table's results panel carries one row per finishing position, so a
# response with only a handful of ranks means the panel was read partially.
# That is the same failure the prompt's "cut off at any edge" rule targets, and
# a prompt rule is unverifiable, so it is enforced here too. The floor is
# inferred from two videos (MPBLfM4mwfE and YzKyFMQ1avU), both of which returned
# nine ranks; revisit it as older or differently-branded broadcasts are
# ingested.
MIN_LADDER_RANKS = 5

_USER_TEXT = "Extract the tournament results panel from this frame."


@dataclass(frozen=True)
class PendingVideo:
    video_id: str
    duration_seconds: int
    consecutive_failures: int


def _find_pending_videos(
    project_id: str,
    dataset: str,
    only_video_ids: list[str] | None = None,
    *,
    client: bigquery.Client | None = None,
) -> list[PendingVideo]:
    """Return videos pending payout extraction.

    A video is pending if it has never been attempted or its latest attempt
    status is 'failed_transient'. Videos with 'complete', 'complete_skipped',
    'failed_permanent', or 'failed_parked' are excluded.

    There is deliberately no NOT EXISTS guard against tournament_results.
    'complete_skipped' is legitimately terminal with zero stage rows, and such a
    filter would mark it permanently pending and reprocess it forever. For the
    same reason the query keys on attempt status only and never consults the
    output table.

    Production callers leave the scope param as None. Integration tests pass
    uuid-scoped lists to constrain the blast radius per CLAUDE.md.
    """
    if client is None:
        client = bigquery.Client(project=project_id)

    video_filter = ""
    params: list = []
    if only_video_ids is not None:
        video_filter = "AND v.video_id IN UNNEST(@only_video_ids)"
        params.append(bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids))

    query = f"""
        WITH attempt_marks AS (
          SELECT
            video_id, status, attempted_at,
            MAX(IF(status NOT LIKE 'failed%', attempted_at, NULL)) OVER (
              PARTITION BY video_id
            ) AS last_non_failure_at
          FROM `{project_id}.{dataset}.tournament_results_processing_attempts`
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
        )
        SELECT
          v.video_id,
          v.duration_seconds,
          COALESCE(a.consecutive_failures, 0) AS consecutive_failures
        FROM `{project_id}.{dataset}.videos` v
        LEFT JOIN attempt_state a USING (video_id)
        WHERE (a.latest_status IS NULL OR a.latest_status = 'failed_transient')
          {video_filter}
        ORDER BY v.video_id
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
    rows = list(client.query(query, job_config=job_config).result())
    return [
        PendingVideo(
            video_id=row.video_id,
            duration_seconds=row.duration_seconds,
            consecutive_failures=row.consecutive_failures,
        )
        for row in rows
    ]


def check_preconditions(video: PendingVideo) -> str | None:
    """Return a skip reason if this video cannot support payout extraction,
    else None.

    Unlike Phases 4 and 5, this check runs in the caller *before* the video is
    downloaded. Those phases accept a post-download check because the download
    amortises across a video's many hands — "a video with tens of hands, one or
    two of which skip, pays nothing extra." Here the entity is the video, so
    there is nothing to amortise over: a skipping video would pay for its own
    full 100-200 MB download to produce zero calls. duration_seconds comes from
    the pending query, so the check needs nothing the download provides.
    """
    if video.duration_seconds <= FRAME_FALLBACK_LADDER[0]:
        return (
            f"skipped: duration_seconds={video.duration_seconds} is not longer than the "
            f"first frame ladder rung ({FRAME_FALLBACK_LADDER[0]}s)"
        )
    return None


def _parse_amount(value: object) -> float | None:
    """Normalise a payout or bounty to a float, or None if unreadable.

    The prompt asks for bare numbers, but the spike observed the same prompt
    returning '$406.25' on one frame and 406.25 on another, so this parses
    defensively rather than relying on it.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lstrip("*").replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_panel(panel: dict) -> dict:
    """Return a copy of the panel with payout and bounty amounts as floats.

    payout_marked falls back to detecting a leading asterisk on the raw value.
    That fallback only fires when the model returned a *string* — the prompt
    asks for numbers, so on the common path the asterisk never reaches Python
    and payout_marked comes from the model alone. It is belt-and-braces against
    the observed string variance, NOT a second source and NOT a cross-check on
    the model's own answer. Do not later read agreement here as corroboration.
    """
    normalized_rows = []
    for row in panel.get("rows") or []:
        raw_payout = row.get("payout")
        marked = bool(row.get("payout_marked"))
        if isinstance(raw_payout, str) and raw_payout.strip().startswith("*"):
            marked = True
        normalized_rows.append({
            **row,
            "payout": _parse_amount(raw_payout),
            "payout_marked": marked,
            "bounty": _parse_amount(row.get("bounty")),
        })
    return {**panel, "rows": normalized_rows}


def _validate_panel(panel: dict) -> None:
    """Raise GeminiPermanentError if the panel cannot produce a usable row.

    Two jobs. First, every REQUIRED scalar column sourced from the model
    response must be non-null: bq_param_type has no None branch and this
    phase's stage writer does not filter None out of the row dict, so a null
    would raise TypeError, classify transient, and retry the video until it
    parks. Extend this if a scalar column is ever added.

    Second, a response of panel_visible true with an empty or short rows array
    would otherwise write a 'complete' row containing no payout data — the one
    thing this phase exists to produce.
    """
    symbol = panel.get("currency_symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise GeminiPermanentError(
            f"panel visible but currency_symbol is not a usable string: {symbol!r}"
        )

    has_bounty_column = panel.get("has_bounty_column")
    if not isinstance(has_bounty_column, bool):
        raise GeminiPermanentError(
            f"panel visible but has_bounty_column is not a boolean: {has_bounty_column!r}"
        )

    rows = panel.get("rows") or []
    if len(rows) < MIN_LADDER_RANKS:
        raise GeminiPermanentError(
            f"panel visible but only {len(rows)} ladder rank(s) returned; "
            f"expected at least {MIN_LADDER_RANKS} — panel was read partially"
        )
    if not any(str(row.get("rank")).strip() == "1" for row in rows):
        raise GeminiPermanentError(
            "panel visible but no rank 1 row returned — panel was read partially"
        )


def _derive_bounty_type(panel: dict) -> str:
    """Map the results panel's Bounty column to a bounty_type.

    Derived from has_bounty_column alone. A corroborating seat-badge signal was
    considered and dropped: this phase runs before Phase 3 and reads an
    arbitrary early frame with no guarantee the poker table is even on screen,
    so the check's most likely firing was a false positive on an unreadable
    table rather than a real contradiction. The prompt compensates by failing
    the frame when the panel is clipped at any edge, which is where a hidden
    Bounty column would otherwise read as an absent one.

    Freezeout ('static') bounty is not modelled — no such event has been
    observed on this channel.
    """
    has_bounty_column = panel.get("has_bounty_column")
    if has_bounty_column is True:
        return "progressive"
    if has_bounty_column is False:
        return "none"
    raise GeminiPermanentError(
        f"has_bounty_column is not a boolean: {has_bounty_column!r}"
    )


async def _extract_with_fallback(
    video: PendingVideo,
    local_video_path: str,
    frame_tmpdir: str,
    extract_results_prompt: str,
    project_id: str,
) -> tuple[int, dict, str]:
    """Return (timestamp, panel, frame_path) for the first rung showing the panel.

    frame_path is the local file the panel was read from. It is returned rather
    than discarded so the caller can retain it in GCS — the frame is the only
    spot-check evidence for a bounty_type that gates prompt selection for every
    hand in the video.

    Rungs at or beyond the video's duration are skipped — ffmpeg would fail on
    every one of them and the video would retry to failed_parked for a
    deterministic condition. check_preconditions guarantees at least one rung
    survives that filter.
    """
    attempted: list[int] = []
    for timestamp in FRAME_FALLBACK_LADDER:
        if timestamp >= video.duration_seconds:
            continue
        attempted.append(timestamp)
        frame_path = os.path.join(frame_tmpdir, f"results_{timestamp:03d}.jpg")
        await asyncio.to_thread(extract_frame, local_video_path, timestamp, frame_path)
        with open(frame_path, "rb") as fh:
            frame_bytes = fh.read()
        panel = await asyncio.to_thread(
            call_gemini_for_frame,
            extract_results_prompt,
            frame_bytes,
            project_id,
            user_text=_USER_TEXT,
            label=f"extract_results_t{timestamp}",
        )
        if panel.get("panel_visible") is True:
            return timestamp, panel, frame_path

    raise GeminiPermanentError(
        f"results panel not visible at any attempted frame timestamp: {attempted}s"
    )


def _transient_status(consecutive_failures: int, max_attempts: int) -> str:
    """Which status to write for a transient failure. The current failure is not
    yet counted in consecutive_failures, hence the +1."""
    return "failed_parked" if consecutive_failures + 1 >= max_attempts else "failed_transient"


def _write_attempt(
    video_id: str,
    status: str,
    status_message: str,
    *,
    project_id: str,
    dataset: str,
) -> None:
    write_tournament_results_processing_attempt_row(
        TournamentResultsProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            video_id=video_id,
            status=status,
            status_message=status_message,
        ),
        project=project_id,
        dataset=dataset,
    )


async def process_video(
    video: PendingVideo,
    local_video_path: str,
    project_id: str,
    dataset: str,
    tournament_results_bucket: str,
    extract_results_prompt: str,
    *,
    max_attempts: int = 3,
) -> str:
    """Process one video end-to-end. Returns the outcome status string.

    Assumes check_preconditions already passed — it runs in the caller, ahead of
    the download.

    Never raises — all exceptions are caught, recorded as attempt rows, and
    translated into a return value so that process_pending_videos can continue
    to the next video.
    """
    try:
        # The frame is uploaded from inside this block, so everything that needs
        # the file on disk has to happen before the tempdir is torn down.
        with tempfile.TemporaryDirectory() as frame_tmpdir:
            timestamp, panel, frame_path = await _extract_with_fallback(
                video, local_video_path, frame_tmpdir, extract_results_prompt, project_id
            )

            # Validate before uploading: there is no reason to retain a frame
            # from a rung that failed, so a permanently-failing video leaves no
            # object behind at all.
            _validate_panel(panel)
            bounty_type = _derive_bounty_type(panel)
            normalized = _normalize_panel(panel)

            # Deterministic path with no ladder rung in it. frame_timestamp_seconds
            # already records which rung won; a timestamped path would orphan the
            # previous object whenever a reprocess succeeded at a different rung.
            # A stable path overwrites cleanly instead — the same property replace
            # semantics give the BQ row.
            frame_gcs_path = f"gs://{tournament_results_bucket}/{video.video_id}/results.jpg"
            # Upload precedes the row write, so a live row's path always resolves.
            await asyncio.to_thread(upload_frame, frame_path, frame_gcs_path, project_id)

            row = TournamentResultsRow(
                video_id=video.video_id,
                bounty_type=bounty_type,
                currency_symbol=panel["currency_symbol"].strip(),
                frame_timestamp_seconds=timestamp,
                frame_gcs_path=frame_gcs_path,
                tournament_results_state={"panel": normalized},
            )
            write_tournament_results(
                [row], video_id=video.video_id, project_id=project_id, dataset=dataset
            )

        status_message = (
            f"complete: panel read at {timestamp}s, {len(normalized['rows'])} ranks, "
            f"bounty_type={bounty_type}"
        )
        # write_tournament_results() has REPLACE (DELETE+INSERT) semantics keyed
        # on video_id, so a failure of the attempt write below is safe:
        # re-running reproduces the same row instead of duplicating it.
        _write_attempt(
            video.video_id, "complete", status_message, project_id=project_id, dataset=dataset
        )
        return "complete"

    except GeminiPermanentError as exc:
        _write_attempt(
            video.video_id,
            "failed_permanent",
            str(exc)[:500],
            project_id=project_id,
            dataset=dataset,
        )
        return "failed_permanent"
    except Exception as exc:
        status = _transient_status(video.consecutive_failures, max_attempts)
        _write_attempt(
            video.video_id, status, str(exc)[:500], project_id=project_id, dataset=dataset
        )
        return status


async def process_pending_videos(
    project_id: str,
    dataset: str,
    videos_bucket: str,
    tournament_results_bucket: str,
    extract_results_prompt: str,
    *,
    video_id: str | None = None,
    max_attempts: int = 3,
    bq_client: bigquery.Client | None = None,
    gcs_client=None,
) -> dict[str, int]:
    """Process all pending videos. Returns summary stats.

    Videos are processed sequentially, one on disk at a time. There is no
    max_concurrent: this phase makes one call per video and the work is
    dominated by the download.
    """
    videos = _find_pending_videos(
        project_id,
        dataset,
        only_video_ids=[video_id] if video_id else None,
        client=bq_client,
    )

    stats: dict[str, int] = {
        "videos_processed": 0,
        "videos_complete": 0,
        "videos_complete_skipped": 0,
        "videos_failed_transient": 0,
        "videos_failed_permanent": 0,
        "videos_failed_parked": 0,
    }

    for video in videos:
        # Ahead of the download on purpose — see check_preconditions.
        skip_reason = check_preconditions(video)
        if skip_reason is not None:
            _write_attempt(
                video.video_id,
                "complete_skipped",
                skip_reason,
                project_id=project_id,
                dataset=dataset,
            )
            stats["videos_processed"] += 1
            stats["videos_complete_skipped"] += 1
            continue

        with tempfile.TemporaryDirectory() as tmpdir:
            local_video_path = os.path.join(tmpdir, f"{video.video_id}.mp4")

            try:
                await asyncio.to_thread(
                    download_video,
                    f"gs://{videos_bucket}/{video.video_id}.mp4",
                    local_video_path,
                    project_id,
                    client=gcs_client,
                )
            except DownloadPermanentError as exc:
                print(
                    f"Video {video.video_id} not found in GCS (permanent): {exc}",
                    file=sys.stderr,
                )
                _write_attempt(
                    video.video_id,
                    "failed_permanent",
                    f"video_download_not_found: {str(exc)[:400]}",
                    project_id=project_id,
                    dataset=dataset,
                )
                stats["videos_processed"] += 1
                stats["videos_failed_permanent"] += 1
                continue
            except Exception as exc:
                print(f"Failed to download video {video.video_id}: {exc}", file=sys.stderr)
                status = _transient_status(video.consecutive_failures, max_attempts)
                _write_attempt(
                    video.video_id,
                    status,
                    f"video_download_failed: {str(exc)[:400]}",
                    project_id=project_id,
                    dataset=dataset,
                )
                stats["videos_processed"] += 1
                stats[f"videos_{status}"] += 1
                continue

            outcome = await process_video(
                video,
                local_video_path,
                project_id,
                dataset,
                tournament_results_bucket,
                extract_results_prompt,
                max_attempts=max_attempts,
            )

        stats["videos_processed"] += 1
        key = f"videos_{outcome}"
        if key in stats:
            stats[key] += 1

    return stats
