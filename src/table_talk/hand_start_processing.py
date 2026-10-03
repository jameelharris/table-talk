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
# Pure, no I/O, and deliberately shared: the chip arithmetic P4-7 needs is the
# same arithmetic P5-5 reads, and restating it here would let the two drift.
from .betting_state import GATE_AMOUNT_TOLERANCE_BB, build_seats
from .card_normalization import normalize_cards
from .frame_extractor import extract_frame
from .frame_uploader import upload_frame
from .gemini_caller import (
    CLIP_MEDIA_RESOLUTION,
    CLIP_MODEL,
    FRAME_MODEL,
    FRAME_RESOLUTION_ULTRA_HIGH,
    GeminiPermanentError,
    call_gemini_for_clip,
    call_gemini_for_frame,
)
from .hand_setup_processing_attempts_writer import write_hand_setup_processing_attempt_row
from .hand_starts_writer import write_hand_starts
from .prompt_context import build_hole_card_context, build_player_context
from .mark_pending import MARK_MESSAGE_PREFIX
from .provenance import build_provenance, select
from .seat_enrichment import add_fva_seat_number, canonical_labels, normalize_heads_up
from .timestamp_utils import parse_timestamp
from .videos_downloader import DownloadPermanentError, download_video

MAX_AVAILABLE_SECONDS = 60
VERIFY_COUNT = 3
VERIFY_INTERVAL = 0.05  # seconds

# Gate identifiers for status_message. The "<gate_id>: <code>: " prefix is fixed
# and must stay stable across releases — the per-gate report groups the attempts
# tables by it, so renaming one silently splits its history in two. The detail
# after the prefix is free text and may change freely.
#
# Not shared with Phase 5: orchestrators are not shared in this codebase, and a
# common constants module would couple two phases that otherwise only meet at a
# table boundary.
GATE_NULL_BOUNTY_PROGRESSIVE = "P4-1: null_bounty_progressive"
GATE_BOUNTY_ON_NON_BOUNTY_VIDEO = "P4-2: bounty_on_non_bounty_video"
GATE_INVALID_LABEL_SET = "P4-3: invalid_label_set"
GATE_INVALID_FVA_ACTION_TYPE = "P4-4: invalid_fva_action_type"
GATE_DUPLICATE_HOLE_CARD = "P4-5: duplicate_hole_card"
GATE_MISSING_HOLE_CARDS_LIVE_SEAT = "P4-6: missing_hole_cards_live_seat"
GATE_FVA_AMOUNT_MISMATCH = "P4-7: fva_amount_mismatch"

# identify_hand_start.md offers exactly these three. fold and check are
# unreachable by definition — the FVA is the first *voluntary chip commitment*,
# and neither commits chips. bet is not in that prompt's vocabulary at all.
VALID_FVA_ACTION_TYPES = frozenset({"call", "raise", "all_in"})


@dataclass(frozen=True)
class PendingHandSetup:
    hand_setup_id: str
    clip_id: str
    video_id: str
    hand_setup_time_seconds: int
    hand_setup_state: dict
    available_seconds: int
    raw_lead_gap_seconds: int
    consecutive_failures: int
    # Query-computed, not a hand_setups column: it comes from the video's
    # tournament_results row. Per CLAUDE.md it belongs on this module-local
    # dataclass rather than being hand-added to the generated row class.
    bounty_type: str


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
    attempt status is 'failed_transient' or 'marked_pending'. hand_setups with
    'complete',
    'complete_skipped', 'complete_uncontested', 'failed_permanent', or
    'failed_parked' are excluded.

    Production callers leave the scope params as None. Integration tests pass
    uuid-scoped lists to constrain the blast radius per CLAUDE.md.
    """
    if client is None:
        client = bigquery.Client(project=project_id)

    video_filter = ""
    hand_setup_filter = ""
    params: list = [
        bigquery.ScalarQueryParameter("max_available_seconds", "INT64", MAX_AVAILABLE_SECONDS),
        # Bound rather than interpolated, so the mark message has exactly one
        # definition and it lives with the code that writes it.
        bigquery.ScalarQueryParameter("mark_message_prefix", "STRING", MARK_MESSAGE_PREFIX),
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
        attempt_marks AS (
          SELECT
            hand_setup_id, status, attempted_at,
            -- A mark is not a failure, so it resets the count rather than
            -- advancing it. New marks say so in `status`; the OR recognises the
            -- pre-'marked_pending' marks already in the table, which are
            -- 'failed_transient' rows carrying the mark message.
            MAX(IF(
              status NOT LIKE 'failed%'
              OR status_message LIKE CONCAT(@mark_message_prefix, '%'),
              attempted_at, NULL
            )) OVER (
              PARTITION BY hand_setup_id
            ) AS last_non_failure_at
          FROM `{project_id}.{dataset}.hand_setup_processing_attempts`
        ),
        attempt_state AS (
          SELECT
            hand_setup_id,
            ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS latest_status,
            COUNTIF(
              status = 'failed_transient'
              AND (last_non_failure_at IS NULL OR attempted_at > last_non_failure_at)
            ) AS consecutive_failures
          FROM attempt_marks
          GROUP BY hand_setup_id
        )
        SELECT
          w.*,
          LEAST(w.raw_lead_gap_seconds, @max_available_seconds) AS available_seconds,
          COALESCE(a.consecutive_failures, 0) AS consecutive_failures,
          tr.bounty_type
        FROM windowed w
        LEFT JOIN attempt_state a USING (hand_setup_id)
        LEFT JOIN `{project_id}.{dataset}.tournament_results` tr ON tr.video_id = w.video_id
        WHERE (a.latest_status IS NULL
               OR a.latest_status IN ('failed_transient', 'marked_pending'))
          {video_filter}
          {hand_setup_filter}
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(client.query(query, job_config=job_config).result())
    # A hand setup whose video has no tournament_results row is a broken
    # invariant, not a data condition: Phase 2's materialization gate records
    # blocked_upstream rather than producing clips, so nothing downstream of it
    # should exist. Raising matches Phase 3, which reaches the same state by the
    # same route. Skipping instead would silently drop the bounty gates.
    for row in rows:
        if row.bounty_type is None:
            raise RuntimeError(
                f"hand setup {row.hand_setup_id} (video {row.video_id}) has no "
                f"tournament_results row; the materialization gate should have "
                f"prevented this. Run `tt extract-payouts --video-id {row.video_id}`."
            )
    return [
        PendingHandSetup(
            hand_setup_id=row.hand_setup_id,
            clip_id=row.clip_id,
            video_id=row.video_id,
            hand_setup_time_seconds=row.hand_setup_time_seconds,
            hand_setup_state=row.hand_setup_state,
            available_seconds=row.available_seconds,
            raw_lead_gap_seconds=row.raw_lead_gap_seconds,
            consecutive_failures=row.consecutive_failures,
            bounty_type=row.bounty_type,
        )
        for row in rows
    ]


def check_preconditions(hand_setup_state: dict, bounty_type: str) -> str | None:
    """Return a skip reason if hand_setup_state can't support hand-start
    processing, else None. Checks run in order; the first failure wins.

    Takes bounty_type explicitly because it is not part of hand_setup_state: it
    belongs to the video's tournament_results row and arrives on the pending
    dataclass. Passing it rather than the whole dataclass keeps this a pure
    function of the two things it actually reads.
    """
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

    # P4-1. Reverses the earlier "a null bounty is a gap, not a skip": a missing
    # badge does not appear on a re-read of the same frame, so this is a skip
    # rather than a retry. It also catches the one phantom-seat shape the
    # null-stack check above cannot — an extra seat carrying a real-looking
    # stack but a null bounty, which is how the documented non-null phantom
    # presented.
    if bounty_type == "progressive":
        missing = [
            p.get("seat_position_label") or "<unknown position>"
            for p in players
            if p.get("bounty") is None or p.get("bounty") <= 0
        ]
        if missing:
            return (
                f"skipped: {GATE_NULL_BOUNTY_PROGRESSIVE}: "
                f"player(s) with no bounty on a progressive video — {', '.join(missing)}"
            )
    else:
        # P4-2. The field is absent, not null, on a non-bounty video — nothing
        # asked for it. A value here means bounty_type was misclassified, which
        # is a property of the video's one payout read and will not change on a
        # retry of this hand.
        present = [
            p.get("seat_position_label") or "<unknown position>"
            for p in players
            if p.get("bounty") is not None
        ]
        if present:
            return (
                f"skipped: {GATE_BOUNTY_ON_NON_BOUNTY_VIDEO}: "
                f"bounty present on bounty_type={bounty_type!r} — {', '.join(present)}"
            )

    # P4-3. Labels come from Phase 3, and re-detecting a clip renumbers every
    # hand in it, so a retry cannot repair this one row — hence a skip.
    #
    # Note what this does NOT catch: a phantom seat whose total_seat_count was
    # inflated to match still presents a self-consistent label set and passes.
    # Seat-count monotonicity is the check for that class, and it is not built.
    expected = set(canonical_labels(total_seat_count))
    actual = [p.get("seat_position_label") for p in players]
    if sorted(actual) != sorted(expected):
        return (
            f"skipped: {GATE_INVALID_LABEL_SET}: "
            f"{total_seat_count}-handed expects {sorted(expected)}, got {sorted(actual)}"
        )

    return None


def check_fva(fva: dict, hand_setup_state: dict) -> str | None:
    """P4-4. Return a reason the FVA is unusable, or None.

    Two conditions, one gate, because both make the same record unusable in the
    same way and both are properties of one step-A answer.

    The label check is not decoration. An unresolvable label leaves seat_number
    None, and the eligible-seat calculation below reads a None seat_number as
    "every seat is eligible" — so a bad label silently widens the hole-card read
    to the whole table. It also defeats build_seats, which marks the pre-FVA
    folds by seat number, so P5-16 would then demand cards for seats that folded
    long before the FVA.
    """
    action_type = fva.get("action_type")
    if action_type not in VALID_FVA_ACTION_TYPES:
        return (
            f"{GATE_INVALID_FVA_ACTION_TYPE}: fva action_type={action_type!r} "
            f"not in {sorted(VALID_FVA_ACTION_TYPES)} — the FVA is a chip commitment"
        )

    label = fva.get("seat_position_label")
    seats = {p.get("seat_position_label") for p in hand_setup_state.get("players", [])}
    if label not in seats:
        return (
            f"{GATE_INVALID_FVA_ACTION_TYPE}: fva seat_position_label={label!r} "
            f"is not a seat in this hand {sorted(s for s in seats if s)}"
        )
    return None


def check_fva_amount(fva: dict, hand_setup_state: dict) -> str | None:
    """P4-7. Return a reason the FVA's amount is impossible, or None.

    The two conditions P5-5 keeps, applied to the one action this phase records:
    an FVA recorded `all_in` must use up the seat's stack, and no FVA may commit
    more than the stack plus the blind it posted. The reverse is deliberately not
    checked here either — an FVA for the whole stack recorded `call` or `raise` is
    a valid reading. See ARCHITECTURE, "P5-5 is one direction, plus
    over-commitment."

    Nothing checked this before, and the cost of that fell on Phase 5.
    `YzKyFMQ1avU_014_003` recorded SB `all_in 17.1`, where 17.1 is the stack
    *after* its 0.5 blind, so the total in front is 17.6 — which is what step D
    then read. The disagreement surfaced as P5-8, and every Phase 5 retry failed
    identically on a Pro call, because the error was a phase upstream of the
    retry. A Phase 4 redo produced 17.6.

    Transient: a random step-A slip, not a property of the window. Phase 4 read
    the same heads-up-blind situation correctly on `013_003`.

    **The arithmetic is `betting_state`'s, not restated.** `build_seats` applies
    `posted_blind_for`, which identifies the blinds by ROLE — heads-up the BTN
    posts the small blind and `normalize_heads_up` has already left no seat
    labelled `SB` at all, so a label lookup would charge the BTN nothing and wave
    through a shove half a blind short. `Seat.chips_remaining` and `.all_in` are
    then the same properties P5-5 reads.

    Runs after P4-4, which is what guarantees the label resolves to a seat.
    """
    bet_amount = fva.get("bet_amount")
    if bet_amount is None:
        return None  # nothing to compare; the FVA's shape is P4-4's business

    seat = build_seats(hand_setup_state, fva)[fva["seat_position_label"]]
    # build_seats seeded street_commitment with the posted blind, which stack_size
    # is already net of; raising it to the FVA's amount is what replay_hand does
    # for any preflop action, so chips_remaining and all_in now mean what they
    # mean in Phase 5.
    seat.street_commitment = max(seat.street_commitment, float(bet_amount))

    if seat.chips_remaining < -GATE_AMOUNT_TOLERANCE_BB:
        return (
            f"{GATE_FVA_AMOUNT_MISMATCH}: {seat.label} {fva.get('action_type')} "
            f"{bet_amount} is {abs(seat.chips_remaining):.2f} BB beyond its stack "
            f"{seat.stack_size:g} plus the {seat.posted_blind:g} it posted"
        )
    if fva.get("action_type") == "all_in" and not seat.all_in:
        return (
            f"{GATE_FVA_AMOUNT_MISMATCH}: {seat.label} all_in {bet_amount} leaves "
            f"{seat.chips_remaining:.2f} BB behind — stack {seat.stack_size:g} plus "
            f"the {seat.posted_blind:g} it posted is "
            f"{seat.stack_size + seat.posted_blind:g} in front"
        )
    return None


def check_duplicate_hole_cards(eligible_players: list[dict]) -> str | None:
    """P4-5. Return a reason two seats hold the same card, or None.

    Compares case-folded. normalize_card only maps "10" -> "T"; it does not
    canonicalize rank or suit case, so "Ah" and "AH" are distinct strings and a
    naive comparison would let a duplicate through.

    Transient: the observed hole-card errors are suit confusions in a stochastic
    read, and a re-attempt re-reads every seat rather than the one that clashed.
    """
    seen: dict[str, str] = {}
    for player in eligible_players:
        for card in player.get("hole_cards") or []:
            if card is None:
                continue
            key = card.casefold()
            if key in seen:
                return (
                    f"{GATE_DUPLICATE_HOLE_CARD}: {card} appears twice — "
                    f"{seen[key]} and {player.get('seat_position_label')}"
                )
            seen[key] = player.get("seat_position_label") or "<unknown position>"
    return None


def check_missing_hole_cards(eligible_players: list[dict], fva_label: str | None) -> str | None:
    """P4-6, narrowed to the FVA seat's own cards. See H5.

    H5 says a missing card costs the hand only on a seat that *stayed in* after
    the FVA, and which seats those are is not knowable here: the actions do not
    exist until step D has run. The FVA seat is the one exception — it is defined
    by a chip commitment, so it stayed in by construction — and that is exactly
    the scope this keeps. Every other seat is P5-16's business.

    This replaces a version that failed the hand for any eligible seat's null.
    Two of its hits had the unreadable seat fold at its first action after the
    FVA, so both hands still carried their whole story.

    Permanent, unlike P4-5. This runs after the in-attempt retry, so a null here
    has already survived a second read of the same frame, and the three observed
    causes — frame-limited illegibility, four-colour suit confusion, and a chat
    overlay covering the cards — are all properties of that frame. Another
    attempt reads the same pixels. The accepted cost is that a hand a different
    verification frame could have resolved is lost; mark-pending is the way back.
    """
    for player in eligible_players:
        if player.get("seat_position_label") != fva_label:
            continue
        cards = player.get("hole_cards")
        if not cards or any(c is None for c in cards):
            return (
                f"{GATE_MISSING_HOLE_CARDS_LIVE_SEAT}: the FVA seat {fva_label} has no "
                f"readable hole cards after retry"
            )
    return None


def eligible_seats_at_fva(hand_start_state: dict) -> list[dict]:
    """The seats still in the hand when the FVA acts.

    Preflop acting order is descending seat number, so "the FVA seat and every
    seat after it" is exactly seat_number <= the FVA's. This is the same set
    build_hole_card_context feeds to the step-C prompt; the two must agree, or
    P5-16 would demand cards for a seat that was never read.
    """
    fva_seat_number = hand_start_state["fva"]["seat_number"]
    players = hand_start_state["hand_setup"].get("players", [])
    if fva_seat_number is None:
        return players
    return [p for p in players if p["seat_number"] <= fva_seat_number]


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


def _transient_status(consecutive_failures: int, max_attempts: int) -> str:
    """Which status to write for a transient failure. The current failure is not
    yet counted in consecutive_failures, hence the +1."""
    return "failed_parked" if consecutive_failures + 1 >= max_attempts else "failed_transient"


async def process_hand_setup(
    hs: PendingHandSetup,
    local_video_path: str,
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_starts_bucket: str,
    identify_hand_start_prompt: str,
    extract_hole_cards_prompt: str,
    *,
    prompt_hashes: dict[str, str],
    max_attempts: int = 3,
) -> str:
    """Process one hand_setup end-to-end. Returns the outcome status string.

    Never raises — all exceptions are caught, recorded as attempt rows, and
    translated into a return value so that process_pending_hand_setups can
    continue to the next hand_setup.
    """
    skip_reason = check_preconditions(hs.hand_setup_state, hs.bounty_type)
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
            entity_id=hs.hand_setup_id,
        )

        if not clip_result.get("found"):
            if clip_result.get("reason") == "uncontested":
                status_message = "complete_uncontested: no voluntary chip commitment"
                write_hand_starts([], hand_setup_id=hs.hand_setup_id, project_id=project_id, dataset=dataset)
                _write_attempt(hs.hand_setup_id, "complete_uncontested", status_message, project_id=project_id, dataset=dataset)
                return "complete_uncontested"
            status = _transient_status(hs.consecutive_failures, max_attempts)
            status_message = f"{status}: {clip_result.get('reason', 'not found')}"
            _write_attempt(hs.hand_setup_id, status, status_message, project_id=project_id, dataset=dataset)
            return status

        fva_time_seconds = parse_timestamp(clip_result["timestamp"])
        _hallucination_guard(fva_time_seconds, hs.hand_setup_time_seconds, hs.available_seconds)

        if clip_result.get("second_action_timestamp") is None:
            status = _transient_status(hs.consecutive_failures, max_attempts)
            status_message = f"{status}: no second action observed within window"
            _write_attempt(hs.hand_setup_id, status, status_message, project_id=project_id, dataset=dataset)
            return status

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
        hand_start_state = {
            "hand_setup": hs.hand_setup_state,
            "fva": fva_data,
            # Sibling of this phase's own contribution. The nested hand_setup
            # carries Phase 3's own provenance block unchanged, so a hand_starts
            # row records both layers without this phase assembling anything.
            "provenance": build_provenance(
                models={"clip": CLIP_MODEL, "frame": FRAME_MODEL},
                # The frame call is step C, which overrides the caller's default
                # per part. Recorded because resolution changes what a read
                # returns and is invisible in the model id and the prompt hash.
                media_resolution={
                    "clip": CLIP_MEDIA_RESOLUTION,
                    "frame": FRAME_RESOLUTION_ULTRA_HIGH,
                },
                prompts=select(
                    prompt_hashes,
                    "prompts/identify_hand_start.md",
                    "prompts/extract_hole_cards.md",
                ),
            ),
        }
        normalize_heads_up(hand_start_state["hand_setup"], fva=hand_start_state["fva"])

        # P4-4 runs after normalize_heads_up, not before: heads-up rewrites the
        # FVA's SB to BTN, and checking the label against the hand's seats ahead
        # of that rewrite would reject every heads-up hand.
        fva_reason = check_fva(hand_start_state["fva"], hand_start_state["hand_setup"])
        if fva_reason is not None:
            status = _transient_status(hs.consecutive_failures, max_attempts)
            _write_attempt(
                hs.hand_setup_id, status, f"{status}: {fva_reason}",
                project_id=project_id, dataset=dataset,
            )
            return status

        # P4-7, on the same step-A answer and also before any frame work: a bad
        # FVA amount must not spend a HIGH-resolution hole-card read, and the
        # error it catches is one Phase 5 can only rediscover at a Pro call.
        #
        # Sequential rather than folded into one loop with P4-4 on purpose:
        # check_fva_amount resolves the FVA's label against the hand's seats, so
        # it must not run on a label P4-4 has just rejected.
        amount_reason = check_fva_amount(
            hand_start_state["fva"], hand_start_state["hand_setup"]
        )
        if amount_reason is not None:
            status = _transient_status(hs.consecutive_failures, max_attempts)
            _write_attempt(
                hs.hand_setup_id, status, f"{status}: {amount_reason}",
                project_id=project_id, dataset=dataset,
            )
            return status

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
                # ULTRA_HIGH is the measured fix for suit misreads on face
                # cards, where the corner pip is small and the artwork is red on
                # every suit. Measured over the four known misreads: the
                # baseline missed 4 of 160 cards and prompt wording moved
                # nothing (3 of 160), while this read 0 of 640 across 20 reps.
                # It costs roughly +1,200 input tokens per read, so it is set
                # per call site rather than on the caller — Phase 3's player
                # info and the payout panel read keep HIGH, where no benefit has
                # been measured. See ARCHITECTURE, "Suit misreads are a
                # resolution problem."
                hole_cards_result = await asyncio.to_thread(
                    call_gemini_for_frame,
                    filled_hole_cards_prompt,
                    frame_bytes,
                    project_id,
                    user_text="Extract hole cards for all eligible players from this frame.",
                    frame_media_resolution=FRAME_RESOLUTION_ULTRA_HIGH,
                    entity_id=hs.hand_setup_id,
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
            hand_setup_players = hand_start_state["hand_setup"].get("players", [])
            eligible_players = eligible_seats_at_fva(hand_start_state)

            if any(p.get("hole_cards") is None for p in eligible_players):
                retry_hole_cards_result = await asyncio.to_thread(
                    call_gemini_for_frame,
                    filled_hole_cards_prompt,
                    frame_bytes,
                    project_id,
                    user_text="Extract hole cards for all eligible players from this frame.",
                    frame_media_resolution=FRAME_RESOLUTION_ULTRA_HIGH,
                    entity_id=hs.hand_setup_id,
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

            # P4-5 then P4-6, in that order and first-failure-wins. A hand with
            # both a duplicate and a null is retried rather than parked: the
            # duplicate is the recoverable half, and giving the permanent gate
            # precedence would discard a hand a retry could still fix.
            #
            # P4-6 judges the FVA seat alone. A null on any other eligible seat
            # is carried into the row and judged by P5-16, once step D has said
            # whether that seat stayed in after the FVA. See H5.
            duplicate_reason = check_duplicate_hole_cards(eligible_players)
            if duplicate_reason is not None:
                status = _transient_status(hs.consecutive_failures, max_attempts)
                _write_attempt(
                    hs.hand_setup_id, status, f"{status}: {duplicate_reason}",
                    project_id=project_id, dataset=dataset,
                )
                return status

            missing_reason = check_missing_hole_cards(
                eligible_players, hand_start_state["fva"]["seat_position_label"]
            )
            if missing_reason is not None:
                _write_attempt(
                    hs.hand_setup_id, "failed_permanent", missing_reason,
                    project_id=project_id, dataset=dataset,
                )
                return "failed_permanent"

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
        # write_hand_starts() is REPLACE (DELETE+INSERT) semantics keyed on
        # hand_setup_id, so a post-write attempt-write failure here is safe:
        # re-running reproduces the same row set instead of duplicating it.
        _write_attempt(hs.hand_setup_id, "complete", status_message, project_id=project_id, dataset=dataset)
        return "complete"

    except GeminiPermanentError as exc:
        _write_attempt(hs.hand_setup_id, "failed_permanent", str(exc)[:500], project_id=project_id, dataset=dataset)
        return "failed_permanent"
    except Exception as exc:
        status = _transient_status(hs.consecutive_failures, max_attempts)
        _write_attempt(hs.hand_setup_id, status, str(exc)[:500], project_id=project_id, dataset=dataset)
        return status


async def process_pending_hand_setups(
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_starts_bucket: str,
    identify_hand_start_prompt: str,
    extract_hole_cards_prompt: str,
    *,
    prompt_hashes: dict[str, str],
    video_id: str | None = None,
    only_hand_setup_ids: list[str] | None = None,
    max_concurrent: int = 4,
    max_attempts: int = 3,
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
        "hand_setups_complete_uncontested": 0,
        "hand_setups_failed_transient": 0,
        "hand_setups_failed_permanent": 0,
        "hand_setups_failed_parked": 0,
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
                    status = _transient_status(hs.consecutive_failures, max_attempts)
                    _write_attempt(
                        hs.hand_setup_id,
                        status,
                        f"video_download_failed: {str(exc)[:400]}",
                        project_id=project_id,
                        dataset=dataset,
                    )
                    stats["hand_setups_processed"] += 1
                    stats[f"hand_setups_{status}"] += 1
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
                        prompt_hashes=prompt_hashes,
                        max_attempts=max_attempts,
                    )

            tasks = [_run_hand_setup(hs) for hs in video_hand_setups]
            outcomes = await asyncio.gather(*tasks)

            for outcome in outcomes:
                stats["hand_setups_processed"] += 1
                key = f"hand_setups_{outcome}"
                if key in stats:
                    stats[key] += 1

    return stats
