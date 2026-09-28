# Phase 5 orchestrator: take a hand_starts row and produce the hand's complete
# voluntary action sequence by street, the community cards revealed on each
# postflop street, and the winning position(s), landing one row in hand_actions.
#
# Per hand: check preconditions, run step D (one video call over the whole hand
# window) to get streets + actions + winning_positions, then run step E — for
# each postflop street D reported, a video scan for the reveal timestamp and a
# HIGH-resolution frame read for the new cards. E's results merge into D's
# street list, frames upload to GCS, and one hand_actions row is written.
#
# D and E run sequentially, not concurrently. E depends on D for the street
# list, and a hand that ends preflop needs zero E calls — under parallelism a
# flop scan would already have fired and returned found: false, wasting a call
# on every preflop-ending hand. Within E the streets are sequential by
# necessity: each scan window starts at the previous street's timestamp, and
# each frame read needs the accumulated prior-card count.
#
# process_hand_start() handles one hand start atomically: either the
# hand_actions row (+ frames in GCS) lands, or none do. Outcomes are recorded in
# hand_start_processing_attempts so re-running the CLI retries transient
# failures and skips completed/skipped/permanently-failed/parked ones.

import asyncio
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass

from google.cloud import bigquery

from ._generated.hand_actions_row import HandActionsRow
from ._generated.hand_start_processing_attempts_row import HandStartProcessingAttemptsRow
from .betting_state import GATE_AMOUNT_TOLERANCE_BB, replay_hand
from .card_normalization import normalize_cards
from .frame_extractor import extract_frame
from .frame_uploader import upload_frame
from .gemini_caller import (
    CLIP_MODEL,
    FRAME_MODEL,
    GeminiPermanentError,
    call_gemini_for_clip,
    call_gemini_for_frame,
)
from .hand_actions_writer import write_hand_actions
from .hand_start_processing_attempts_writer import write_hand_start_processing_attempt_row
from .prompt_context import build_action_context, build_fva_context, build_prior_cards_context
from .provenance import build_provenance, select
from .reference_images import STREET_REFERENCE_ORDER, reference_image_filename
from .seat_enrichment import heads_up_label
from .timestamp_utils import parse_timestamp
from .videos_downloader import DownloadPermanentError, download_video

# At fps=1.0 a window longer than this exceeds Gemini's 256-frame limit for a
# single video part. It is a technical ceiling, not a preference — and it is a
# precondition skip rather than a truncation, because a truncated window turns a
# hand whose river lands past the cap into a record showing the hand ending on
# the turn: a silent wrong answer instead of a loud failure.
MAX_WINDOW_SECONDS = 240

# The scan returns the moment cards become stationary; half a second of settle
# clears the tail of the dealing animation.
FRAME_SETTLE_OFFSET = 0.5

# Reads of one street's cards before the hand is failed. Community cards are
# centre-frame with no chip occlusion, so a null is far more likely to be jitter
# than a legibility limit — and a re-read costs ~$0.004 against ~$0.078 for the
# whole-hand re-run that failing forces.
CARD_READ_ATTEMPTS = 3

# Extra scans issued when a street scan contradicts D. The two directions of
# scan error are not symmetric: a wrong found: true is caught by
# _street_timestamp_guard, but a wrong found: false truncates the hand, looks
# legitimate, and had no guard at all — it was visible only because D happened
# to disagree. Reproduction against a real window showed the miss is stochastic
# rather than a prompt defect: 3 of 4 runs found the street the first scan had
# missed. Later-street windows are also the narrowest, so this is the cheapest
# place in the phase to buy a retry and the most expensive place to lose one.
SCAN_RETRY_ON_DISAGREEMENT = 1

POSTFLOP_STREETS = ("flop", "turn", "river")

# D3. Why a street's community_cards are what they are, recorded per street so
# an empty array is never ambiguous between "no cards exist", "not looked for"
# and "looked for and not found".
EXTRACTION_NOT_APPLICABLE = "not_applicable"   # preflop: no community cards exist
EXTRACTION_EXTRACTED = "extracted"             # step E located the street and read it
EXTRACTION_SKIPPED_INERT = "skipped_inert"     # not scanned by design, per D1
# Defined so the vocabulary is complete, not because it is written: after P5-14
# an unread contested street fails the attempt, and an inert one is never
# attempted. If this value ever appears in the corpus, something regressed.
EXTRACTION_UNREAD = "unread"

# Gate identifiers for status_message. The "<gate_id>: <code>: " prefix is fixed
# and must stay stable across releases — the per-gate report groups the attempts
# table by it, so renaming one silently splits its history in two.
#
# P5-15 is out of sequence on purpose. The numbering is the spec's allocation
# order, not this file's execution order, and P5-11 (board card count) turned
# out to be already implemented as _street_cards_unusable, so its number is
# retired rather than recycled — reusing it would make the report ambiguous
# between two different checks.
GATE_ACTION_LABEL_UNRESOLVED = "P5-1: action_label_unresolved"
GATE_WINNER_UNRESOLVED = "P5-2: winner_unresolved"
GATE_ACTION_AFTER_HAND_END = "P5-3: action_after_hand_end"
GATE_ACTION_AFTER_FOLD_OR_ALL_IN = "P5-4: action_after_fold_or_all_in"
GATE_ALL_IN_MISMATCH = "P5-5: all_in_mismatch"
GATE_IMPLAUSIBLE_WINNER = "P5-6: implausible_winner"
GATE_ILLEGAL_BETTING = "P5-7: illegal_betting"
GATE_FVA_MISMATCH = "P5-8: fva_mismatch"
GATE_AMOUNT_TYPE_MISMATCH = "P5-9: amount_type_mismatch"
GATE_UNKNOWN_STREET = "P5-15: unknown_street"
GATE_DUPLICATE_BOARD_CARD = "P5-12: duplicate_board_card"
GATE_STREET_TIMESTAMP_ORDER = "P5-13: street_timestamp_order"
GATE_CONTESTED_STREET_UNREAD = "P5-14: contested_street_unread"

# betting_state reports structure; this maps its vocabulary onto the gates that
# own each one, so the replay stays ignorant of status messages.
_VIOLATION_GATES = {
    "action_label_unresolved": GATE_ACTION_LABEL_UNRESOLVED,
    "action_after_hand_end": GATE_ACTION_AFTER_HAND_END,
    "action_after_fold_or_all_in": GATE_ACTION_AFTER_FOLD_OR_ALL_IN,
    "unknown_street": GATE_UNKNOWN_STREET,
}

# Chip commitments. fold and check move nothing and are excluded everywhere an
# amount is required to be positive.
_COMMITTING_TYPES = frozenset({"call", "bet", "raise", "all_in"})

# What the FVA can be, mirroring identify_hand_start.md. Duplicated rather than
# imported from Phase 4: the two phases meet at a table boundary, not in code.
_FVA_TYPES = frozenset({"call", "raise", "all_in"})

# extract_community_cards_from_frame.md's count rule, mirrored here so a
# short read is caught rather than stored as a malformed board.
EXPECTED_NEW_CARDS = {0: 3, 3: 1, 4: 1}


class CommunityCardUnreadable(Exception):
    """A street's cards could not be read cleanly within CARD_READ_ATTEMPTS.

    Classified transient: a later prompt fix could make the card readable, and
    terminal classification would foreclose that.
    """


@dataclass(frozen=True)
class PendingHandStart:
    hand_start_id: str
    hand_setup_id: str
    clip_id: str
    video_id: str
    hand_setup_time_seconds: int
    fva_time_seconds: int
    hand_start_state: dict
    raw_lead_gap_seconds: int
    consecutive_failures: int


def _find_pending_hand_starts(
    project_id: str,
    dataset: str,
    only_video_ids: list[str] | None = None,
    only_hand_start_ids: list[str] | None = None,
    *,
    client: bigquery.Client | None = None,
) -> list[PendingHandStart]:
    """Return hand_starts rows pending hand-action processing.

    A hand start is pending if it has never been attempted or its latest attempt
    status is 'failed_transient'.

    The window bound is the LEAD over hand_setups, computed across *all* of a
    video's hand setups before joining to hand_starts: the next hand setup
    bounds this hand even when that setup was skipped, uncontested or parked and
    so has no hand_starts row of its own. Computing LEAD after the join would
    silently widen windows past the next hand.

    There is deliberately no NOT EXISTS guard against hand_actions — several
    outcomes are legitimately terminal with zero stage rows, and such a filter
    would mark them permanently pending.

    Production callers leave the scope params as None. Integration tests pass
    uuid-scoped lists to constrain the blast radius per CLAUDE.md.
    """
    if client is None:
        client = bigquery.Client(project=project_id)

    video_filter = ""
    hand_start_filter = ""
    params: list = []
    if only_video_ids is not None:
        video_filter = "AND h.video_id IN UNNEST(@only_video_ids)"
        params.append(bigquery.ArrayQueryParameter("only_video_ids", "STRING", only_video_ids))
    if only_hand_start_ids is not None:
        hand_start_filter = "AND h.hand_start_id IN UNNEST(@only_hand_start_ids)"
        params.append(
            bigquery.ArrayQueryParameter("only_hand_start_ids", "STRING", only_hand_start_ids)
        )

    query = f"""
        WITH windowed AS (
          SELECT
            hs.hand_setup_id,
            hs.hand_setup_time_seconds,
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
            hand_start_id, status, attempted_at,
            MAX(IF(status NOT LIKE 'failed%', attempted_at, NULL)) OVER (
              PARTITION BY hand_start_id
            ) AS last_non_failure_at
          FROM `{project_id}.{dataset}.hand_start_processing_attempts`
        ),
        attempt_state AS (
          SELECT
            hand_start_id,
            ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS latest_status,
            COUNTIF(
              status = 'failed_transient'
              AND (last_non_failure_at IS NULL OR attempted_at > last_non_failure_at)
            ) AS consecutive_failures
          FROM attempt_marks
          GROUP BY hand_start_id
        )
        SELECT
          h.hand_start_id,
          h.hand_setup_id,
          h.clip_id,
          h.video_id,
          h.hand_start_state,
          h.fva_time_seconds,
          w.hand_setup_time_seconds,
          w.raw_lead_gap_seconds,
          COALESCE(a.consecutive_failures, 0) AS consecutive_failures
        FROM `{project_id}.{dataset}.hand_starts` h
        INNER JOIN windowed w USING (hand_setup_id)
        LEFT JOIN attempt_state a USING (hand_start_id)
        WHERE (a.latest_status IS NULL OR a.latest_status = 'failed_transient')
          {video_filter}
          {hand_start_filter}
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(client.query(query, job_config=job_config).result())
    return [
        PendingHandStart(
            hand_start_id=row.hand_start_id,
            hand_setup_id=row.hand_setup_id,
            clip_id=row.clip_id,
            video_id=row.video_id,
            hand_setup_time_seconds=row.hand_setup_time_seconds,
            fva_time_seconds=row.fva_time_seconds,
            hand_start_state=row.hand_start_state,
            raw_lead_gap_seconds=row.raw_lead_gap_seconds,
            consecutive_failures=row.consecutive_failures,
        )
        for row in rows
    ]


def check_preconditions(hs: PendingHandStart) -> str | None:
    """Return a skip reason if this hand start can't support action processing,
    else None. Checks run in order; the first failure wins.

    Phase 4's null-stack, null-label, total_seat_count and pot_size_bb checks are
    deliberately not repeated — a hand_starts row exists only because Phase 4
    already passed them.
    """
    if hs.raw_lead_gap_seconds > MAX_WINDOW_SECONDS:
        return (
            f"skipped: raw_lead_gap_seconds={hs.raw_lead_gap_seconds} exceeds "
            f"MAX_WINDOW_SECONDS={MAX_WINDOW_SECONDS}; window cannot contain the whole hand"
        )

    fva = hs.hand_start_state.get("fva")
    if not fva or fva.get("seat_position_label") is None:
        return (
            "skipped: fva missing or has null seat_position_label "
            "— no anchor for action_order 1"
        )

    return None


class StreetTimestampUnusable(Exception):
    """A street reveal timestamp that cannot be right. Classified transient."""


def _street_timestamp_guard(
    street_timestamp: int, scan_start: int, window_end: int, street_name: str,
    *, strict_after: bool,
) -> None:
    """P5-13. A reveal timestamp outside the scanned window, or not after the
    previous street's, is a hallucination.

    Without this, a bad timestamp yields a frame from the wrong moment and a
    plausible-looking wrong board — the silent failure this design exists to
    avoid. Mirrors Phase 4's FVA hallucination guard.

    **Strict, not merely within-window.** `scan_start` is the previous street's
    timestamp, so the existing bound already gave non-strict monotonicity; what
    it allowed was two streets revealed in the same second, which cannot happen
    — every scanned street now has betting between it and the one before, since
    the runouts that had none are skipped as inert.

    `strict_after` is False for the flop, whose scan_start is the FVA rather
    than a previous reveal: a flop can legitimately land on the same second as
    the first voluntary action.

    **Transient, where this used to be permanent.** The classification changed
    with the strictness. The only hallucinated timestamp ever observed — one
    flop in the original corpus run — was caught here and then *resolved on a
    rerun*, so permanent was the wrong class for the single instance there is
    evidence about. A permanent failure also denies the retry to a defect that
    reproduction showed to be stochastic.
    """
    lower_ok = scan_start < street_timestamp if strict_after else scan_start <= street_timestamp
    if not (lower_ok and street_timestamp <= window_end):
        bound = "after" if strict_after else "at or after"
        raise StreetTimestampUnusable(
            f"{GATE_STREET_TIMESTAMP_ORDER}: {street_name} timestamp "
            f"{street_timestamp}s is not {bound} {scan_start}s and within "
            f"{window_end}s — treating as hallucination"
        )


def _board_duplicate(prior_cards: list, new_cards: list) -> str | None:
    """P5-12. A card cannot appear twice on one board.

    Case-folded: normalize_card maps "10" to "T" and does nothing else, so "Ah"
    and "AH" are distinct strings and a naive comparison would let a duplicate
    through.

    Board-against-hole-card duplicates are deliberately NOT checked here. A
    Phase 5 retry re-reads only the board, and the documented case had the error
    in the hole card — retrying would spend Pro calls re-reading the half that
    was right. Those stay in dbt and go through the manual-correction workflow.
    """
    seen = {c.casefold() for c in prior_cards if c is not None}
    for card in new_cards:
        if card is None:
            continue
        key = card.casefold()
        if key in seen:
            return f"{GATE_DUPLICATE_BOARD_CARD}: {card} already on the board"
        seen.add(key)
    return None


def _street_cards_unusable(new_cards: list, prior_count: int) -> str | None:
    """Return why a street's card read is unusable, or None if it is fine."""
    expected = EXPECTED_NEW_CARDS.get(prior_count)
    if expected is None:
        return f"unexpected prior-card count {prior_count}"
    if len(new_cards) != expected:
        return f"expected {expected} new card(s), got {len(new_cards)}"
    if any(card is None for card in new_cards):
        return "null card in read"
    return None


def _check_amount_types(replay) -> str | None:
    """P5-9. fold and check move no chips; everything else moves some."""
    for street in replay.streets:
        for a in street.actions:
            if a.action_type in ("fold", "check"):
                if abs(a.bet_amount) > GATE_AMOUNT_TOLERANCE_BB:
                    return (
                        f"{GATE_AMOUNT_TYPE_MISMATCH}: {street.street_name} action "
                        f"{a.action_order} {a.seat_label} {a.action_type} carries "
                        f"bet_amount {a.bet_amount}, expected 0"
                    )
            elif a.action_type in _COMMITTING_TYPES:
                if a.bet_amount <= 0:
                    return (
                        f"{GATE_AMOUNT_TYPE_MISMATCH}: {street.street_name} action "
                        f"{a.action_order} {a.seat_label} {a.action_type} carries "
                        f"bet_amount {a.bet_amount}, expected greater than 0"
                    )
            if a.bet_amount < 0:
                return (
                    f"{GATE_AMOUNT_TYPE_MISMATCH}: {street.street_name} action "
                    f"{a.action_order} {a.seat_label} has negative bet_amount "
                    f"{a.bet_amount}"
                )
    return None


def _check_all_in(replay) -> str | None:
    """P5-5. All-in if and only if the stack runs out, and never more than it.

    Both directions. Forward catches an all_in that leaves chips behind; reverse
    catches a shove recorded as a raise, which is how a transposed raise/all-in
    pair presents. Over-commitment is a third shape and gets its own message —
    a seat cannot put in more than it holds.
    """
    for street in replay.streets:
        for a in street.actions:
            if a.chips_remaining_after < -GATE_AMOUNT_TOLERANCE_BB:
                return (
                    f"{GATE_ALL_IN_MISMATCH}: {street.street_name} action "
                    f"{a.action_order} {a.seat_label} commits {a.bet_amount} but is "
                    f"{abs(a.chips_remaining_after):.2f} BB beyond its stack"
                )
            if a.action_type == "all_in" and not a.exhausts_stack:
                return (
                    f"{GATE_ALL_IN_MISMATCH}: {street.street_name} action "
                    f"{a.action_order} {a.seat_label} all_in {a.bet_amount} leaves "
                    f"{a.chips_remaining_after:.2f} BB behind"
                )
            if a.action_type in _COMMITTING_TYPES and a.action_type != "all_in":
                if a.exhausts_stack:
                    return (
                        f"{GATE_ALL_IN_MISMATCH}: {street.street_name} action "
                        f"{a.action_order} {a.seat_label} {a.action_type} "
                        f"{a.bet_amount} exhausts the stack but is not recorded "
                        f"all_in"
                    )
    return None


def _check_betting_legality(replay) -> str | None:
    """P5-7(a). Each action against what the seat was actually facing.

    Minimum raise sizes are deliberately not checked: display rounding and
    incomplete all-in raises make them unenforceable against extracted amounts.
    Turn order — P5-7(b) — is not checked here either; it is deferred to dbt.
    """
    for street in replay.streets:
        for a in street.actions:
            owed, largest = a.owed_before, a.largest_before
            where = (
                f"{GATE_ILLEGAL_BETTING}: (a) {street.street_name} action "
                f"{a.action_order} {a.seat_label}"
            )
            if a.action_type == "fold":
                continue  # always legal
            if a.action_type == "all_in":
                continue  # committing everything is legal in any spot
            if a.action_type == "check":
                if owed > GATE_AMOUNT_TOLERANCE_BB:
                    return f"{where} checked while owing {owed:.2f} BB"
            elif a.action_type == "call":
                short = a.bet_amount < largest - GATE_AMOUNT_TOLERANCE_BB
                if short and not a.exhausts_stack:
                    return (
                        f"{where} called {a.bet_amount} against {largest} without "
                        f"being all-in for less"
                    )
            elif a.action_type == "bet":
                if street.street_name == "preflop":
                    return f"{where} bet preflop, where the blind means something is owed"
                if largest > GATE_AMOUNT_TOLERANCE_BB:
                    return f"{where} bet into an existing commitment of {largest}"
            elif a.action_type == "raise":
                if a.bet_amount <= largest + GATE_AMOUNT_TOLERANCE_BB:
                    return f"{where} raised to {a.bet_amount}, not above {largest}"
    return None


def _check_street_completeness(replay) -> str | None:
    """P5-7(c), scoped to live streets.

    A live street is one where betting had not already closed — two or more
    seats still in and more than one holding chips, per D1. On an inert street
    the opposite is true and the rule inverts: a called all-in runout leaves the
    deeper caller holding chips with nothing to do, and requiring it to act
    would fail every correct runout hand. Measured at 9 of 133 hands before this
    scoping was added.

    Only streets with a successor are checked. If the hand carried on, this
    street's betting must have finished.
    """
    inert = set(replay.inert_streets())
    for street in replay.streets[:-1]:
        if street.street_name in inert:
            continue
        acted = {a.seat_label for a in street.actions}
        silent = [s for s in street.seats_with_chips if s not in acted]
        if silent:
            return (
                f"{GATE_ILLEGAL_BETTING}: (c) {street.street_name} ended with "
                f"{', '.join(sorted(silent))} still in holding chips and never "
                f"acting, but a later street follows"
            )
    return None


def _check_fva_cross_check(replay, fva: dict) -> str | None:
    """P5-8. Step D's first preflop action against the block it was handed.

    Reverses the earlier judgement that this belongs in dbt rather than the
    orchestrator. Under a Phase 6 gate a flagged hand is excluded anyway, so a
    retry is the only route by which it can ever land.
    """
    preflop = replay.street("preflop")
    if preflop is None or not preflop.actions:
        return None
    first = preflop.actions[0]

    if first.action_type not in _FVA_TYPES:
        return (
            f"{GATE_FVA_MISMATCH}: preflop action 1 is {first.action_type}, which "
            f"commits no chips and cannot be the first voluntary action"
        )
    if first.seat_label != fva.get("seat_position_label"):
        return (
            f"{GATE_FVA_MISMATCH}: preflop action 1 is {first.seat_label}, but the "
            f"fva block says {fva.get('seat_position_label')}"
        )
    if first.action_type != str(fva.get("action_type") or "").strip().lower():
        return (
            f"{GATE_FVA_MISMATCH}: preflop action 1 is {first.action_type}, but the "
            f"fva block says {fva.get('action_type')}"
        )
    expected = fva.get("bet_amount")
    if expected is not None and abs(first.bet_amount - float(expected)) > GATE_AMOUNT_TOLERANCE_BB:
        return (
            f"{GATE_FVA_MISMATCH}: preflop action 1 commits {first.bet_amount}, but "
            f"the fva block says {expected}"
        )
    return None


def _check_winners(replay, winning_positions: list[str], seat_labels: set[str]) -> str | None:
    """P5-2 resolution and P5-6 plausibility. Emptiness is checked earlier."""
    seen = set()
    for label in winning_positions:
        if label not in seat_labels:
            return (
                f"{GATE_WINNER_UNRESOLVED}: winner {label!r} is not a seat in this "
                f"hand {sorted(seat_labels)}"
            )
        if label in seen:
            return f"{GATE_WINNER_UNRESOLVED}: winner {label!r} appears twice"
        seen.add(label)

    still_in = set(replay.seats_still_in_at_end)
    outside = seen - still_in
    if outside:
        return (
            f"{GATE_IMPLAUSIBLE_WINNER}: {', '.join(sorted(outside))} won without "
            f"being in the hand at the end (still in: {sorted(still_in)})"
        )
    if replay.hand_ended_on is not None and seen != still_in:
        return (
            f"{GATE_IMPLAUSIBLE_WINNER}: the hand ended by folds on "
            f"{replay.hand_ended_on} leaving {sorted(still_in)}, but the winners "
            f"are {sorted(seen)}"
        )
    if len(seen) > 1 and replay.hand_ended_on is not None:
        return (
            f"{GATE_IMPLAUSIBLE_WINNER}: {len(seen)} winners on a hand that ended "
            f"by folds"
        )
    return None


def inert_streets_for(hand_setup_state: dict, fva: dict, streets: list[dict]) -> frozenset[str]:
    """Street names whose community cards carry no decisions, per D1.

    Replays the hand a second time rather than having check_step_d_output hand
    its state out. The replay is pure and costs microseconds, and the gate stays
    a function that returns a reason or None — which is what every one of its
    tests asserts against.

    Only meaningful once the gates have passed: a hand whose replay hit a
    violation has no trustworthy state to derive inertness from.
    """
    return frozenset(replay_hand(hand_setup_state, fva, streets).inert_streets())


def check_step_d_output(
    hand_setup_state: dict, fva: dict, streets: list[dict], winning_positions: list[str]
) -> str | None:
    """Every step-D gate, in order, first failure wins. None means it passed.

    Runs before any step E call, so a hand that fails costs one clip call rather
    than up to seven. Structural problems come first: once the replay cannot
    walk the sequence, every derived number after it is meaningless, so there is
    nothing to be gained by reporting a second opinion on it.
    """
    replay = replay_hand(hand_setup_state, fva, streets)
    if replay.violation is not None:
        gate = _VIOLATION_GATES[replay.violation.code]
        return f"{gate}: {replay.violation.detail}"

    seat_labels = {
        p.get("seat_position_label") for p in hand_setup_state.get("players", [])
    }
    for reason in (
        _check_amount_types(replay),
        _check_all_in(replay),
        _check_betting_legality(replay),
        _check_street_completeness(replay),
        _check_fva_cross_check(replay, fva),
        _check_winners(replay, winning_positions, seat_labels),
    ):
        if reason is not None:
            return reason
    return None


def _write_attempt(
    hand_start_id: str,
    status: str,
    status_message: str,
    *,
    project_id: str,
    dataset: str,
) -> None:
    write_hand_start_processing_attempt_row(
        HandStartProcessingAttemptsRow(
            attempt_id=uuid.uuid4().hex,
            hand_start_id=hand_start_id,
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


async def _scan_for_street(
    street_name: str,
    scan_prompt: str,
    video_gcs_uri: str,
    scan_start: int,
    window_end: int,
    project_id: str,
    reference_images: list[tuple[bytes, str, str]] | None,
) -> dict:
    """Scan for one street's reveal, asking again if the answer contradicts D.

    Only ever called for a street D reported, so a found: false here is a
    disagreement rather than the ordinary end-of-hand answer — that one never
    reaches this function, because postflop_streets is built from D's own street
    list. Retrying every found: false would add a call to nearly every hand for
    nothing, since found: false is the correct answer on any hand ending before
    the river.

    D's claim gates only *whether to ask again*, never what the answer is. If
    every scan says no, E's answer stands: D's list is demonstrably fallible in
    this direction too, having once reported a flop on a hand that folded out
    preflop, where E was right to find none.
    """
    scan_result: dict = {}
    for attempt in range(SCAN_RETRY_ON_DISAGREEMENT + 1):
        scan_result = await asyncio.to_thread(
            call_gemini_for_clip,
            scan_prompt.replace("{street_name}", street_name),
            video_gcs_uri,
            scan_start,
            window_end,
            project_id,
            user_text=f"Scan this clip and find when the {street_name} cards appear.",
            reference_images=reference_images,
            # Retries carry their own label so their cost is greppable apart
            # from first-attempt scans.
            label=f"step_e_scan_{street_name}" + ("_retry" if attempt else ""),
        )
        if scan_result.get("found"):
            return scan_result
    return scan_result


async def _read_street_cards(
    frame_prompt: str,
    frame_bytes: bytes,
    prior_cards: list[str],
    project_id: str,
    street_name: str,
) -> list[str]:
    """Read one street's new community cards, retrying an unusable read.

    Raises CommunityCardUnreadable once the attempt cap is spent. Raising here —
    rather than returning a partial answer — is what keeps a null out of the
    caller's prior_cards accumulator.
    """
    filled_prompt = frame_prompt.replace(
        "{prior_cards}", build_prior_cards_context(prior_cards)
    )
    reason = None
    for _ in range(CARD_READ_ATTEMPTS):
        result = await asyncio.to_thread(
            call_gemini_for_frame,
            filled_prompt,
            frame_bytes,
            project_id,
            user_text="Identify the new community cards visible in this frame.",
            label=f"step_e_read_{street_name}",
        )
        new_cards = normalize_cards(result.get("new_cards") or [])
        # Count and null check first, then the board-duplicate check: both are
        # read failures the same retry can fix, so they share the attempt cap
        # rather than each getting their own.
        reason = _street_cards_unusable(new_cards, len(prior_cards)) or _board_duplicate(
            prior_cards, new_cards
        )
        if reason is None:
            return new_cards
    raise CommunityCardUnreadable(
        f"{street_name}: {reason} after {CARD_READ_ATTEMPTS} reads"
    )


async def _run_step_e(
    hs: PendingHandStart,
    local_video_path: str,
    video_gcs_uri: str,
    project_id: str,
    postflop_streets: list[str],
    window_end: int,
    scan_prompt: str,
    frame_prompt: str,
    reference_images: list[tuple[bytes, str, str]] | None,
    frame_tmpdir: str,
    inert_streets: frozenset[str],
) -> tuple[dict, str | None]:
    """Resolve each postflop street's reveal timestamp and new community cards.

    Returns (resolved, truncated_at) where resolved maps street_name to
    (street_timestamp, new_cards, local_frame_path), and truncated_at names the
    first street D reported that the scan could not find — which is not an
    error, it is how E learns the hand ended there.
    """
    resolved: dict[str, tuple[int, list[str], str]] = {}
    prior_cards: list[str] = []

    # The flop cannot precede the first voluntary action, so scanning starts at
    # the FVA rather than at hand_setup_time_seconds (which is what the PoC
    # used). Same answer, narrower window, cheaper call — do not widen it back.
    current_scan_start = hs.fva_time_seconds

    for street_name in postflop_streets:
        # P5-10. An inert street carries no decisions, so its cards have no
        # analytical value and are not worth a ~13K-token scan plus a read. No
        # call, no frame, no upload.
        #
        # Inert streets are always a suffix — D1 closure is monotone forward —
        # so skipping them never leaves prior_cards short for a street that is
        # still scanned, and the accumulator-keyed card-count rule is unaffected.
        if street_name in inert_streets:
            continue

        # A found: false in this loop always contradicts D — see _scan_for_street.
        # Neither prior_cards nor current_scan_start is touched until a scan has
        # succeeded, so a retry that succeeds advances them exactly as a
        # first-attempt success does, and one that fails leaves them untouched.
        scan_result = await _scan_for_street(
            street_name,
            scan_prompt,
            video_gcs_uri,
            current_scan_start,
            window_end,
            project_id,
            reference_images,
        )
        if not scan_result.get("found"):
            return resolved, street_name

        street_timestamp = parse_timestamp(scan_result["timestamp"])
        # The flop's scan_start is the FVA, not a previous reveal, so it may
        # legitimately share a second with it.
        _street_timestamp_guard(
            street_timestamp, current_scan_start, window_end, street_name,
            strict_after=bool(prior_cards),
        )

        local_path = os.path.join(frame_tmpdir, f"{street_name}.jpg")
        await asyncio.to_thread(
            extract_frame,
            local_video_path,
            street_timestamp + FRAME_SETTLE_OFFSET,
            local_path,
        )
        with open(local_path, "rb") as fh:
            frame_bytes = fh.read()

        # _read_street_cards raises rather than returning a null-bearing list,
        # so prior_cards below only ever grows by a fully-read street. The PoC
        # filtered nulls out of the accumulator instead, which left the next
        # street's read believing there were fewer prior cards than there were
        # and broke the 0/3/4 -> 3/1/1 count rule.
        new_cards = await _read_street_cards(
            frame_prompt, frame_bytes, prior_cards, project_id, street_name
        )

        prior_cards = prior_cards + new_cards
        resolved[street_name] = (street_timestamp, new_cards, local_path)
        current_scan_start = street_timestamp

    return resolved, None


async def process_hand_start(
    hs: PendingHandStart,
    local_video_path: str,
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_actions_bucket: str,
    extract_player_actions_prompt: str,
    extract_community_cards_prompt: str,
    extract_community_cards_from_frame_prompt: str,
    reference_images: list[tuple[bytes, str, str]] | None = None,
    *,
    prompt_hashes: dict[str, str],
    max_attempts: int = 3,
) -> str:
    """Process one hand start end-to-end. Returns the outcome status string.

    Never raises — all exceptions are caught, recorded as attempt rows, and
    translated into a return value so that process_pending_hand_starts can
    continue to the next hand start.
    """
    skip_reason = check_preconditions(hs)
    if skip_reason is not None:
        _write_attempt(
            hs.hand_start_id, "complete_skipped", skip_reason,
            project_id=project_id, dataset=dataset,
        )
        return "complete_skipped"

    try:
        video_gcs_uri = f"gs://{videos_bucket}/{hs.video_id}.mp4"
        window_end = hs.hand_setup_time_seconds + hs.raw_lead_gap_seconds
        total_seat_count = hs.hand_start_state.get("hand_setup", {}).get("total_seat_count")

        # --- Step D: the whole hand's action sequence in one video call ---
        filled_actions_prompt = (
            extract_player_actions_prompt
            .replace("{player_context}", build_action_context(hs.hand_start_state))
            .replace(
                "{fva_context}",
                build_fva_context(hs.hand_start_state["fva"], hs.fva_time_seconds),
            )
        )
        d_result = await asyncio.to_thread(
            call_gemini_for_clip,
            filled_actions_prompt,
            video_gcs_uri,
            hs.hand_setup_time_seconds,
            window_end,
            project_id,
            user_text="Extract the complete voluntary action sequence from this video clip.",
            label="step_d_player_actions",
        )

        winning_positions = [
            heads_up_label(label, total_seat_count)
            for label in (d_result.get("winning_positions") or [])
        ]

        # raw_lead_gap_seconds bounds the hand from above but does not prove the
        # window contained its end. winning_positions does: D derives it by
        # watching the pot pushed toward a seat, so an empty array means the pot
        # award was never observed. Checked before any step E call, so a
        # truncated window costs one call rather than seven.
        if not winning_positions:
            status = _transient_status(hs.consecutive_failures, max_attempts)
            _write_attempt(
                hs.hand_start_id, status,
                f"{status}: no winning position observed — window may not contain hand end",
                project_id=project_id, dataset=dataset,
            )
            return status

        actions_by_street = {}
        for street in d_result.get("streets") or []:
            actions = street.get("actions") or []
            for action in actions:
                action["seat_position_label"] = heads_up_label(
                    action.get("seat_position_label"), total_seat_count
                )
            actions_by_street[street.get("street_name")] = actions

        # Every step-D gate, before any step E call. A hand that fails here
        # costs one clip call instead of up to seven, and a transient failure
        # gets the retry that is the only way a bad step-D read can be
        # recovered — dbt can flag it but never re-extract it.
        gate_reason = check_step_d_output(
            hs.hand_start_state["hand_setup"],
            hs.hand_start_state["fva"],
            d_result.get("streets") or [],
            winning_positions,
        )
        if gate_reason is not None:
            status = _transient_status(hs.consecutive_failures, max_attempts)
            _write_attempt(
                hs.hand_start_id, status, f"{status}: {gate_reason}",
                project_id=project_id, dataset=dataset,
            )
            return status

        # Safe to derive only now: the gates above have established that the
        # replay walked the whole sequence without contradiction.
        inert = inert_streets_for(
            hs.hand_start_state["hand_setup"],
            hs.hand_start_state["fva"],
            d_result.get("streets") or [],
        )

        # Iterate the canonical order rather than D's, so a mis-ordered response
        # cannot make the turn scan run before the flop's timestamp is known.
        postflop_streets = [s for s in POSTFLOP_STREETS if s in actions_by_street]

        with tempfile.TemporaryDirectory() as frame_tmpdir:
            # --- Step E: per postflop street, scan for the reveal then read it ---
            resolved, truncated_at = await _run_step_e(
                hs,
                local_video_path,
                video_gcs_uri,
                project_id,
                postflop_streets,
                window_end,
                extract_community_cards_prompt,
                extract_community_cards_from_frame_prompt,
                reference_images,
                frame_tmpdir,
                inert,
            )

            # P5-14. A street D reported, that is not inert, and that E could
            # not locate. This used to be recorded in status_message and the row
            # written anyway as `complete` — a hand that looks finished and ends
            # a street early, which corrupts an aggregate rather than thinning
            # it. Failing the attempt buys the retry instead.
            #
            # The population is far smaller than the raw truncation rate
            # suggests: inert streets are no longer scanned at all, and the
            # latest corpus run's truncations were all inert.
            if truncated_at is not None:
                scans = SCAN_RETRY_ON_DISAGREEMENT + 1
                status = _transient_status(hs.consecutive_failures, max_attempts)
                _write_attempt(
                    hs.hand_start_id, status,
                    f"{status}: {GATE_CONTESTED_STREET_UNREAD}: D reported "
                    f"{truncated_at} but {scans} scans found none, and betting "
                    f"was still live there",
                    project_id=project_id, dataset=dataset,
                )
                return status

            # --- Merge E into D ---
            #
            # Iterates every street D reported rather than stopping at the first
            # one absent from `resolved`. An inert street is deliberately absent
            # and must still be recorded, so a `break` here would silently drop
            # the whole runout.
            streets = []
            if "preflop" in actions_by_street:
                streets.append({
                    "street_name": "preflop",
                    "street_timestamp": None,  # no card-reveal moment preflop
                    "community_cards": [],
                    "actions": actions_by_street["preflop"],
                    "extraction_status": EXTRACTION_NOT_APPLICABLE,
                })
            for street_name in POSTFLOP_STREETS:
                if street_name not in actions_by_street:
                    continue  # D did not report it
                if street_name in inert:
                    streets.append({
                        "street_name": street_name,
                        "street_timestamp": None,
                        "community_cards": [],
                        "actions": actions_by_street[street_name],
                        "extraction_status": EXTRACTION_SKIPPED_INERT,
                    })
                    continue
                street_timestamp, community_cards, _ = resolved[street_name]
                streets.append({
                    "street_name": street_name,
                    "street_timestamp": street_timestamp,
                    "community_cards": community_cards,
                    "actions": actions_by_street[street_name],
                    "extraction_status": EXTRACTION_EXTRACTED,
                })

            # Step E's prompts and reference images are listed only if step E
            # actually ran. A hand that ended preflop makes no scan and no read,
            # so naming those files would claim a provenance the row does not
            # have. `resolved` is empty in exactly that case.
            step_e_ran = bool(resolved)
            prompt_files = ["prompts/extract_player_actions.md"]
            models = {"clip": CLIP_MODEL}
            if step_e_ran:
                prompt_files += [
                    "prompts/extract_community_cards.md",
                    "prompts/extract_community_cards_from_frame.md",
                    *(
                        f"references/{reference_image_filename(street)}"
                        for street in STREET_REFERENCE_ORDER
                    ),
                ]
                # The card reads are frame-mode calls; the scans are clip-mode.
                models["frame"] = FRAME_MODEL

            hand_action_state = {
                "hand_start": hs.hand_start_state,
                "streets": streets,
                "winning_positions": winning_positions,
                "provenance": build_provenance(
                    models=models,
                    prompts=select(prompt_hashes, *prompt_files),
                ),
            }

            # Uploads precede the row write, so a live row's paths always resolve.
            street_frame_gcs_paths = []
            for street in streets:
                street_name = street["street_name"]
                if street_name not in resolved:
                    continue
                _, _, local_path = resolved[street_name]
                gcs_path = (
                    f"gs://{hand_actions_bucket}/{hs.video_id}/{hs.clip_id}/"
                    f"{hs.hand_setup_id}/{street_name}.jpg"
                )
                await asyncio.to_thread(upload_frame, local_path, gcs_path, project_id)
                street_frame_gcs_paths.append(gcs_path)

            write_hand_actions(
                [
                    HandActionsRow(
                        hand_start_id=hs.hand_start_id,
                        hand_setup_id=hs.hand_setup_id,
                        clip_id=hs.clip_id,
                        video_id=hs.video_id,
                        hand_action_state=hand_action_state,
                        street_frame_gcs_paths=street_frame_gcs_paths,
                    )
                ],
                hand_start_id=hs.hand_start_id,
                project_id=project_id,
                dataset=dataset,
            )

        street_names = ",".join(street["street_name"] for street in streets)
        status_message = f"complete: window={hs.raw_lead_gap_seconds}s streets={street_names}"
        if truncated_at is not None:
            # D and E disagreeing is the one cross-check sequential execution
            # preserves; record it rather than discarding it.
            scans = SCAN_RETRY_ON_DISAGREEMENT + 1
            status_message += (
                f"; D reported {truncated_at} but {scans} scans found none"
                f" — truncated there"
            )
        # write_hand_actions() is REPLACE (DELETE+INSERT) semantics keyed on
        # hand_start_id, so a post-write attempt-write failure here is safe:
        # re-running reproduces the same row instead of duplicating it.
        _write_attempt(
            hs.hand_start_id, "complete", status_message,
            project_id=project_id, dataset=dataset,
        )
        return "complete"

    except GeminiPermanentError as exc:
        _write_attempt(
            hs.hand_start_id, "failed_permanent", str(exc)[:500],
            project_id=project_id, dataset=dataset,
        )
        return "failed_permanent"
    except (CommunityCardUnreadable, StreetTimestampUnusable) as exc:
        status = _transient_status(hs.consecutive_failures, max_attempts)
        _write_attempt(
            hs.hand_start_id, status, f"{status}: {str(exc)[:480]}",
            project_id=project_id, dataset=dataset,
        )
        return status
    except Exception as exc:
        status = _transient_status(hs.consecutive_failures, max_attempts)
        _write_attempt(
            hs.hand_start_id, status, str(exc)[:500],
            project_id=project_id, dataset=dataset,
        )
        return status


async def process_pending_hand_starts(
    project_id: str,
    dataset: str,
    videos_bucket: str,
    hand_actions_bucket: str,
    extract_player_actions_prompt: str,
    extract_community_cards_prompt: str,
    extract_community_cards_from_frame_prompt: str,
    reference_images: list[tuple[bytes, str, str]] | None = None,
    *,
    prompt_hashes: dict[str, str],
    video_id: str | None = None,
    only_hand_start_ids: list[str] | None = None,
    max_concurrent: int = 4,
    max_attempts: int = 3,
    bq_client: bigquery.Client | None = None,
    gcs_client=None,
) -> dict[str, int]:
    """Process all pending hand starts. Returns summary stats.

    Videos are processed sequentially (one on disk at a time). Hand starts
    within each video are processed concurrently up to max_concurrent.
    """
    hand_starts = _find_pending_hand_starts(
        project_id,
        dataset,
        only_video_ids=[video_id] if video_id else None,
        only_hand_start_ids=only_hand_start_ids,
        client=bq_client,
    )

    by_video: dict[str, list[PendingHandStart]] = {}
    for hs in hand_starts:
        by_video.setdefault(hs.video_id, []).append(hs)

    stats: dict[str, int] = {
        "hand_starts_processed": 0,
        "hand_starts_complete": 0,
        "hand_starts_complete_skipped": 0,
        "hand_starts_failed_transient": 0,
        "hand_starts_failed_permanent": 0,
        "hand_starts_failed_parked": 0,
    }

    for vid, video_hand_starts in by_video.items():
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
                for hs in video_hand_starts:
                    _write_attempt(
                        hs.hand_start_id,
                        "failed_permanent",
                        f"video_download_not_found: {str(exc)[:400]}",
                        project_id=project_id,
                        dataset=dataset,
                    )
                    stats["hand_starts_processed"] += 1
                    stats["hand_starts_failed_permanent"] += 1
                continue
            except Exception as exc:
                print(f"Failed to download video {vid}: {exc}", file=sys.stderr)
                for hs in video_hand_starts:
                    status = _transient_status(hs.consecutive_failures, max_attempts)
                    _write_attempt(
                        hs.hand_start_id,
                        status,
                        f"video_download_failed: {str(exc)[:400]}",
                        project_id=project_id,
                        dataset=dataset,
                    )
                    stats["hand_starts_processed"] += 1
                    stats[f"hand_starts_{status}"] += 1
                continue

            sem = asyncio.Semaphore(max_concurrent)

            async def _run_hand_start(hs: PendingHandStart) -> str:
                async with sem:
                    return await process_hand_start(
                        hs,
                        local_video_path,
                        project_id,
                        dataset,
                        videos_bucket,
                        hand_actions_bucket,
                        extract_player_actions_prompt,
                        extract_community_cards_prompt,
                        extract_community_cards_from_frame_prompt,
                        reference_images,
                        prompt_hashes=prompt_hashes,
                        max_attempts=max_attempts,
                    )

            tasks = [_run_hand_start(hs) for hs in video_hand_starts]
            outcomes = await asyncio.gather(*tasks)

            for outcome in outcomes:
                stats["hand_starts_processed"] += 1
                key = f"hand_starts_{outcome}"
                if key in stats:
                    stats[key] += 1

    return stats
