# Phase 4 orchestrator: identify hand starts (first voluntary chip commitment
# + second action) within hand_setups windows, extract hole cards, and land
# rows in hand_starts.
#
# process_hand_setup() handles one hand_setup atomically: either the
# hand_starts row (+ frames in GCS) lands, or none do. Outcomes are recorded
# in hand_setup_processing_attempts so re-running the CLI retries transient
# failures and skips completed/skipped/permanently-failed ones.
#
# process_pending_hand_setups() coordinates across all pending hand_setups,
# downloading each video once and processing its hand_setups concurrently.

import asyncio
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass

from google.cloud import bigquery

from ._generated.hand_setup_processing_attempts_row import HandSetupProcessingAttemptsRow
from ._generated.hand_starts_row import HandStartsRow
from .card_normalization import normalize_cards
from .frame_extractor import extract_frame
from .frame_uploader import upload_frame
from .gemini_caller import GeminiPermanentError, call_gemini_for_clip, call_gemini_for_frame
from .hand_setup_processing_attempts_writer import write_hand_setup_processing_attempt_row
from .hand_starts_writer import write_hand_starts
from .prompt_context import build_hole_card_context, build_player_context
from .seat_enrichment import add_fva_seat_number, normalize_heads_up
from .timestamp_utils import parse_timestamp
from .videos_downloader import DownloadPermanentError, download_video

MAX_AVAILABLE_SECONDS = 60
VERIFY_COUNT = 3
VERIFY_INTERVAL = 0.05  # seconds


@dataclass(frozen=True)
class PendingHandSetup:
    hand_setup_id: str
    clip_id: str
    video_id: str
    hand_setup_time_seconds: int
    hand_setup_state: dict
    available_seconds: int
    raw_lead_gap_seconds: int


def _find_pending_hand_setups(
    project_id: str,
    dataset: str,
    only_video_ids: list[str] | None = None,
    only_hand_setup_ids: list[str] | None = None,
    *,
    client: bigquery.Client | None = None,
) -> list[PendingHandSetup]:
    """Return hand_setups rows pending hand-start processing.

    A hand_setup is pending if it has never been attempted or its latest
    attempt status is 'failed_transient'. hand_setups with 'complete',
    'complete_skipped', or 'failed_permanent' are excluded.

    Production callers leave the scope params as None. Integration tests pass
    uuid-scoped lists to constrain the blast radius per CLAUDE.md.
    """
    if client is None:
        client = bigquery.Client(project=project_id)

    video_filter = ""
    hand_setup_filter = ""
    params: list = [
        bigquery.ScalarQueryParameter("max_available_seconds", "INT64", MAX_AVAILABLE_SECONDS)
    ]
    if only_video_ids is not None:
        video_filter = "AND w.video_id IN UNNEST(@only_video_ids)"
        params.append(bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids))
    if only_hand_setup_ids is not None:
        hand_setup_filter = "AND w.hand_setup_id IN UNNEST(@only_hand_setup_ids)"
        params.append(bigquery.ArrayQueryParameter("only_hand_setup_ids", "STRING", only_hand_setup_ids))

    query = f"""
        WITH windowed AS (
          SELECT
            hs.*,
            COALESCE(
              LEAD(hs.hand_setup_time_seconds) OVER (
                PARTITION BY hs.video_id
                ORDER BY hs.hand_setup_time_seconds, hs.hand_setup_id
              ),
              v.duration_seconds
            ) AS hand_setup_end_time_seconds,
            COALESCE(
              LEAD(hs.hand_setup_time_seconds) OVER (
                PARTITION BY hs.video_id
                ORDER BY hs.hand_setup_time_seconds, hs.hand_setup_id
              ),
              v.duration_seconds
            ) - hs.hand_setup_time_seconds AS raw_lead_gap_seconds
          FROM `{project_id}.{dataset}.hand_setups` hs
          INNER JOIN `{project_id}.{dataset}.videos` v USING (video_id)
        ),
        latest_attempts AS (
          SELECT
            hand_setup_id, status,
            ROW_NUMBER() OVER (PARTITION BY hand_setup_id ORDER BY attempted_at DESC) AS rn
          FROM `{project_id}.{dataset}.hand_setup_processing_attempts`
        )
        SELECT
          w.*,
          LEAST(w.raw_lead_gap_seconds, @max_available_seconds) AS available_seconds
        FROM windowed w
        LEFT JOIN latest_attempts la
          ON w.hand_setup_id = la.hand_setup_id AND la.rn = 1
        WHERE (la.status IS NULL OR la.status = 'failed_transient')
          {video_filter}
          {hand_setup_filter}
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(client.query(query, job_config=job_config).result())
    return [
        PendingHandSetup(
            hand_setup_id=row.hand_setup_id,
            clip_id=row.clip_id,
            video_id=row.video_id,
            hand_setup_time_seconds=row.hand_setup_time_seconds,
            hand_setup_state=row.hand_setup_state,
            available_seconds=row.available_seconds,
            raw_lead_gap_seconds=row.raw_lead_gap_seconds,
        )
        for row in rows
    ]


def check_preconditions(hand_setup_state: dict) -> str | None:
    """Return a skip reason if hand_setup_state can't support hand-start
    processing, else None. Checks run in order; the first failure wins."""
    players = hand_setup_state.get("players", [])

    null_stack_labels = [
        p.get("seat_position_label") or "<unknown position>"
        for p in players if p.get("stack_size") is None
    ]
    if null_stack_labels:
        return f"skipped: player(s) with null stack_size — {', '.join(null_stack_labels)}"

    null_label_stacks = [p["stack_size"] for p in players if p.get("seat_position_label") is None]
    if null_label_stacks:
        identifiers = ", ".join(f"stack={s}" for s in null_label_stacks)
        return f"skipped: player(s) with null seat_position_label — {identifiers}"

    total_seat_count = hand_setup_state.get("total_seat_count")
    if total_seat_count is None or total_seat_count < 2:
        return f"skipped: total_seat_count={total_seat_count} (< 2)"

    pot_size_bb = hand_setup_state.get("pot_size_bb")
    if not pot_size_bb:  # covers both None and 0
        return f"skipped: pot_size_bb={pot_size_bb!r} (zero or null)"

    return None


def _hallucination_guard(fva_seconds: int, hand_setup_time: int, available_seconds: int) -> None:
    if not (hand_setup_time <= fva_seconds <= hand_setup_time + available_seconds):
        raise GeminiPermanentError(
            f"FVA timestamp {fva_seconds}s outside window "
            f"[{hand_setup_time}, {hand_setup_time + available_seconds}] — treating as hallucination"
        )


def _write_attempt(
    hand_setup_id: str,
    status: str,
    status_message: str,
    *,
    project_id: str,
    dataset: str,
) -> None:
    write_hand_setup_processing_attempt_row(
        HandSetupProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_setup_id=hand_setup_id,
            status=status,
            status_message=status_message,
        ),
        project=project_id,
        dataset=dataset,
    )


async def process_hand_setup(
    hs: PendingHandSetup,
    local_video_path: str,
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_starts_bucket: str,
    identify_hand_start_prompt: str,
    extract_hole_cards_prompt: str,
) -> str:
    """Process one hand_setup end-to-end. Returns the outcome status string.

    Never raises — all exceptions are caught, recorded as attempt rows, and
    translated into a return value so that process_pending_hand_setups can
    continue to the next hand_setup.
    """
    skip_reason = check_preconditions(hs.hand_setup_state)
    if skip_reason is not None:
        _write_attempt(hs.hand_setup_id, "complete_skipped", skip_reason, project_id=project_id, dataset=dataset)
        return "complete_skipped"

    try:
        video_gcs_uri = f"gs://{videos_bucket}/{hs.video_id}.mp4"
        filled_identify_prompt = (
            identify_hand_start_prompt
            .replace("{player_context}", build_player_context(hs.hand_setup_state))
            .replace("{available_seconds}", str(hs.available_seconds))
        )
        clip_result = await asyncio.to_thread(
            call_gemini_for_clip,
            filled_identify_prompt,
            video_gcs_uri,
            hs.hand_setup_time_seconds,
            hs.hand_setup_time_seconds + hs.available_seconds,
            project_id,
            user_text="Identify the first voluntary chip commitment and second action in this video window.",
        )

        if not clip_result.get("found"):
            if clip_result.get("reason") == "uncontested":
                status_message = "complete: uncontested — no voluntary chip commitment"
                write_hand_starts([], hand_setup_id=hs.hand_setup_id, project_id=project_id, dataset=dataset)
                _write_attempt(hs.hand_setup_id, "complete", status_message, project_id=project_id, dataset=dataset)
                return "complete"
            status_message = f"failed_transient: {clip_result.get('reason', 'not found')}"
            _write_attempt(hs.hand_setup_id, "failed_transient", status_message, project_id=project_id, dataset=dataset)
            return "failed_transient"

        fva_time_seconds = parse_timestamp(clip_result["timestamp"])
        _hallucination_guard(fva_time_seconds, hs.hand_setup_time_seconds, hs.available_seconds)

        if clip_result.get("second_action_timestamp") is None:
            status_message = "failed_transient: no second action observed within window"
            _write_attempt(hs.hand_setup_id, "failed_transient", status_message, project_id=project_id, dataset=dataset)
            return "failed_transient"

        second_action_time_seconds = parse_timestamp(clip_result["second_action_timestamp"])

        fva_data = {
            "seat_position_label": clip_result.get("seat_position_label"),
            "action_type": clip_result.get("action_type"),
            "bet_amount": clip_result.get("bet_amount"),
        }
        add_fva_seat_number(fva_data)

        # hand_start_state["hand_setup"] is the same dict object as
        # hs.hand_setup_state (PendingHandSetup is frozen, but its dict
        # field isn't) — normalize_heads_up and the hole-card matching below
        # mutate it in place, so hs.hand_setup_state is mutated too. Harmless
        # today since nothing reads hs after this point.
        hand_start_state = {"hand_setup": hs.hand_setup_state, "fva": fva_data}
        normalize_heads_up(hand_start_state["hand_setup"], fva=hand_start_state["fva"])

        with tempfile.TemporaryDirectory() as frame_tmpdir:
            async def _extract_verify_frames() -> list[str]:
                paths = []
                for n in range(VERIFY_COUNT):
                    ts = second_action_time_seconds + VERIFY_INTERVAL * (n + 1)
                    local_path = os.path.join(frame_tmpdir, f"verify_{n:03d}.jpg")
                    await asyncio.to_thread(extract_frame, local_video_path, ts, local_path)
                    paths.append(local_path)
                return paths

            async def _run_step_c() -> tuple[str, str, bytes, dict]:
                fva_frame_local_path = os.path.join(frame_tmpdir, "fva.jpg")
                await asyncio.to_thread(extract_frame, local_video_path, fva_time_seconds, fva_frame_local_path)
                with open(fva_frame_local_path, "rb") as fh:
                    frame_bytes = fh.read()
                filled_hole_cards_prompt = (
                    extract_hole_cards_prompt
                    .replace("{hole_card_context}", build_hole_card_context(hand_start_state))
                )
                hole_cards_result = await asyncio.to_thread(
                    call_gemini_for_frame,
                    filled_hole_cards_prompt,
                    frame_bytes,
                    project_id,
                    user_text="Extract hole cards for all eligible players from this frame.",
                )
                return fva_frame_local_path, filled_hole_cards_prompt, frame_bytes, hole_cards_result

            (
                verify_frame_local_paths,
                (fva_frame_local_path, filled_hole_cards_prompt, frame_bytes, hole_cards_result),
            ) = await asyncio.gather(_extract_verify_frames(), _run_step_c())

            hole_cards_by_label = {
                p.get("seat_position_label"): p for p in hole_cards_result.get("players", [])
            }
            for player in hand_start_state["hand_setup"].get("players", []):
                matched = hole_cards_by_label.get(player.get("seat_position_label"))
                if matched and matched.get("hole_cards") is not None:
                    player["hole_cards"] = normalize_cards(matched["hole_cards"])
                else:
                    player["hole_cards"] = None

            # Step C is non-deterministic: the same frame + prompt sometimes
            # returns hole_cards: null for an eligible seat even when the
            # card is legible (it never returns a wrong card, only null). A
            # narrower single-seat retry prompt was tested and rejected — it
            # returned another player's cards mislabeled onto the retried
            # seat — so the retry reuses this exact frame/prompt, fills gaps
            # only, and never overwrites a non-null first-call answer.
            fva_seat_number = hand_start_state["fva"]["seat_number"]
            hand_setup_players = hand_start_state["hand_setup"].get("players", [])
            eligible_players = (
                hand_setup_players if fva_seat_number is None
                else [p for p in hand_setup_players if p["seat_number"] <= fva_seat_number]
            )

            if any(p.get("hole_cards") is None for p in eligible_players):
                retry_hole_cards_result = await asyncio.to_thread(
                    call_gemini_for_frame,
                    filled_hole_cards_prompt,
                    frame_bytes,
                    project_id,
                    user_text="Extract hole cards for all eligible players from this frame.",
                )
                retry_by_label = {
                    p.get("seat_position_label"): p for p in retry_hole_cards_result.get("players", [])
                }
                for player in hand_setup_players:
                    if player.get("hole_cards") is not None:
                        continue  # first call is authoritative — retry only fills gaps
                    matched = retry_by_label.get(player.get("seat_position_label"))
                    if matched and matched.get("hole_cards") is not None:
                        player["hole_cards"] = normalize_cards(matched["hole_cards"])

            residual_null_labels = [
                p.get("seat_position_label") for p in eligible_players if p.get("hole_cards") is None
            ]

            fva_frame_gcs_path = (
                f"gs://{hand_starts_bucket}/{hs.video_id}/{hs.clip_id}/{hs.hand_setup_id}/fva.jpg"
            )
            await asyncio.to_thread(upload_frame, fva_frame_local_path, fva_frame_gcs_path, project_id)

            verify_frame_gcs_paths = []
            for n, local_path in enumerate(verify_frame_local_paths):
                gcs_path = (
                    f"gs://{hand_starts_bucket}/{hs.video_id}/{hs.clip_id}/{hs.hand_setup_id}/verify_{n:03d}.jpg"
                )
                await asyncio.to_thread(upload_frame, local_path, gcs_path, project_id)
                verify_frame_gcs_paths.append(gcs_path)

            write_hand_starts(
                [
                    HandStartsRow(
                        hand_start_id=f"{hs.hand_setup_id}_001",
                        hand_setup_id=hs.hand_setup_id,
                        clip_id=hs.clip_id,
                        video_id=hs.video_id,
                        fva_time_seconds=fva_time_seconds,
                        second_action_time_seconds=second_action_time_seconds,
                        hand_start_state=hand_start_state,
                        fva_frame_gcs_path=fva_frame_gcs_path,
                        verify_frame_gcs_paths=verify_frame_gcs_paths,
                    )
                ],
                hand_setup_id=hs.hand_setup_id,
                project_id=project_id,
                dataset=dataset,
            )

        if hs.raw_lead_gap_seconds > hs.available_seconds:
            status_message = (
                f"complete: available_seconds={hs.available_seconds} "
                f"(capped from raw_lead_gap={hs.raw_lead_gap_seconds})"
            )
        else:
            status_message = f"complete: available_seconds={hs.available_seconds}"
        if residual_null_labels:
            status_message += f"; null hole_cards after retry — {', '.join(residual_null_labels)}"
        # write_hand_starts() is REPLACE (DELETE+INSERT) semantics keyed on
        # hand_setup_id, so a post-write attempt-write failure here is safe:
        # re-running reproduces the same row set instead of duplicating it.
        _write_attempt(hs.hand_setup_id, "complete", status_message, project_id=project_id, dataset=dataset)
        return "complete"

    except GeminiPermanentError as exc:
        _write_attempt(hs.hand_setup_id, "failed_permanent", str(exc)[:500], project_id=project_id, dataset=dataset)
        return "failed_permanent"
    except Exception as exc:
        _write_attempt(hs.hand_setup_id, "failed_transient", str(exc)[:500], project_id=project_id, dataset=dataset)
        return "failed_transient"


async def process_pending_hand_setups(
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_starts_bucket: str,
    identify_hand_start_prompt: str,
    extract_hole_cards_prompt: str,
    *,
    video_id: str | None = None,
    only_hand_setup_ids: list[str] | None = None,
    max_concurrent: int = 4,
    bq_client: bigquery.Client | None = None,
    gcs_client=None,
) -> dict[str, int]:
    """Process all pending hand_setups. Returns summary stats.

    Videos are processed sequentially (one on disk at a time). hand_setups
    within each video are processed concurrently up to max_concurrent.
    """
    hand_setups = _find_pending_hand_setups(
        project_id,
        dataset,
        only_video_ids=[video_id] if video_id else None,
        only_hand_setup_ids=only_hand_setup_ids,
        client=bq_client,
    )

    by_video: dict[str, list[PendingHandSetup]] = {}
    for hs in hand_setups:
        by_video.setdefault(hs.video_id, []).append(hs)

    stats: dict[str, int] = {
        "hand_setups_processed": 0,
        "hand_setups_complete": 0,
        "hand_setups_complete_skipped": 0,
        "hand_setups_failed_transient": 0,
        "hand_setups_failed_permanent": 0,
    }

    for vid, video_hand_setups in by_video.items():
        with tempfile.TemporaryDirectory() as tmpdir:
            local_video_path = os.path.join(tmpdir, f"{vid}.mp4")

            try:
                await asyncio.to_thread(
                    download_video,
                    f"gs://{videos_bucket}/{vid}.mp4",
                    local_video_path,
                    project_id,
                    client=gcs_client,
                )
            except DownloadPermanentError as exc:
                print(f"Video {vid} not found in GCS (permanent): {exc}", file=sys.stderr)
                for hs in video_hand_setups:
                    _write_attempt(
                        hs.hand_setup_id,
                        "failed_permanent",
                        f"video_download_not_found: {str(exc)[:400]}",
                        project_id=project_id,
                        dataset=dataset,
                    )
                    stats["hand_setups_processed"] += 1
                    stats["hand_setups_failed_permanent"] += 1
                continue
            except Exception as exc:
                print(f"Failed to download video {vid}: {exc}", file=sys.stderr)
                for hs in video_hand_setups:
                    _write_attempt(
                        hs.hand_setup_id,
                        "failed_transient",
                        f"video_download_failed: {str(exc)[:400]}",
                        project_id=project_id,
                        dataset=dataset,
                    )
                    stats["hand_setups_processed"] += 1
                    stats["hand_setups_failed_transient"] += 1
                continue

            sem = asyncio.Semaphore(max_concurrent)

            async def _run_hand_setup(hs: PendingHandSetup) -> str:
                async with sem:
                    return await process_hand_setup(
                        hs,
                        local_video_path,
                        project_id,
                        dataset,
                        videos_bucket,
                        hand_starts_bucket,
                        identify_hand_start_prompt,
                        extract_hole_cards_prompt,
                    )

            tasks = [_run_hand_setup(hs) for hs in video_hand_setups]
            outcomes = await asyncio.gather(*tasks)

            for outcome in outcomes:
                stats["hand_setups_processed"] += 1
                key = f"hand_setups_{outcome}"
                if key in stats:
                    stats[key] += 1

    return stats
