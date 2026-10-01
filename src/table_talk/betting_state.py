# The running state of one poker hand, replayed from step D's action sequence.
#
# Pure: no I/O, no Gemini, no BigQuery. Everything it knows comes from a hand's
# setup blob, its FVA block, and the streets step D returned.
#
# Two jobs, and they are the same walk:
#
#   D2 — the running state. After each action, per seat: chips remaining,
#        folded, all-in; per street: the largest commitment, the seats still in,
#        and the seats still holding chips.
#   D1 — the inert-street rule. Whether betting closed on a street with no
#        further action possible, which makes every later street's community
#        cards analytically worthless and not worth a scan.
#
# The gates in Phase 5 read this; nothing here decides retry policy or writes an
# attempt row. Violations are reported, not raised, because the caller owns the
# mapping to a gate id and a status.
#
# WHAT THIS DELIBERATELY DOES NOT CHECK
#
# Turn order. Whether each action came from the seat whose turn it was is
# deferred to dbt — all-ins, incomplete raises and reopened betting make it the
# highest false-positive surface in the ruleset, and a false positive here parks
# a good hand. See CLAUDE.md on proving complex rules in dbt before promoting
# them. Nothing in this module computes postflop acting order, and that absence
# is the point: adding it would invite the check.
#
# Minimum raise sizes, likewise — display rounding and incomplete all-in raises
# make them unenforceable against extracted amounts.

from dataclasses import dataclass, field

# Tolerance for every chip comparison here. Deliberately separate from, and
# looser than, the tolerance the dbt layer will calibrate: that one decides
# whether a hand is *usable*, this one decides whether to spend Pro retries on a
# hand and eventually park it. A false positive here is recoverable — an
# operator can mark-pending and re-run — but it is quiet, and it bills a Pro
# call per hand to undo.
#
# Displayed values are rounded (an SB with 4.6 behind shoved a recorded 4.55)
# and the rounding accumulates across streets, so this starts at double the
# single observed discrepancy. Phase 6 must not reuse this constant.
GATE_AMOUNT_TOLERANCE_BB = 0.1

STREET_ORDER = ("preflop", "flop", "turn", "river")

# The blinds, in big blinds, as they sit in front of a seat. The ante is
# excluded on purpose: it leaves the seat rather than sitting in front of it, so
# it is already netted out of stack_size and never enters this arithmetic. See
# ARCHITECTURE on the big blind ante — the whole table's ante is posted by the
# BB, and subtracting it per seat is a documented way to get this wrong.
BIG_BLIND = 1.0
SMALL_BLIND = 0.5


@dataclass(frozen=True)
class Violation:
    """A structural problem that stops the replay. `code` maps to a gate id in
    the caller; `detail` names the seat, street and values involved."""

    code: str
    detail: str


@dataclass
class Seat:
    label: str
    seat_number: int
    stack_size: float
    posted_blind: float
    # The blind sitting in front of this seat on the CURRENT street. Equal to
    # posted_blind preflop and zero on every street after it: the blind is in
    # front of the seat only for the street it was posted on, and a postflop
    # bet_amount carries no blind at all. Subtracting posted_blind on every
    # street understates the chips a postflop shove moved and makes a genuinely
    # all-in seat read as still holding its blind.
    street_blind: float = 0.0
    folded: bool = False
    # Chips that have left the stack on streets already completed.
    committed_earlier: float = 0.0
    # Highest bet_amount seen on the current street. Highest, not last: a seat
    # that bets 3 and then folds to a raise still committed 3, and its fold
    # carries bet_amount 0.0.
    street_commitment: float = 0.0

    @property
    def committed_total(self) -> float:
        """Chips gone from the stack, including the current street.

        The posted blind is subtracted because stack_size is already net of it:
        a seat showing 58 with 58.5 in front has committed exactly its 58.
        """
        current = max(self.street_commitment - self.street_blind, 0.0)
        return self.committed_earlier + current

    @property
    def chips_remaining(self) -> float:
        return self.stack_size - self.committed_total

    @property
    def all_in(self) -> bool:
        return self.chips_remaining <= GATE_AMOUNT_TOLERANCE_BB

    @property
    def can_act(self) -> bool:
        return not self.folded and not self.all_in


@dataclass(frozen=True)
class ActionReplay:
    """One action and the state it met."""

    street_name: str
    action_order: int
    seat_label: str
    action_type: str
    bet_amount: float
    # all_in resolved against the running state: a bet, a call (possibly for
    # less) or a raise. The stored action_type is never rewritten — this is for
    # the checks only.
    interpreted_type: str
    # Largest commitment on this street before the action, and what this seat
    # still owed to match it.
    largest_before: float
    owed_before: float
    # True when the amount committed exhausts the seat's stack, whatever the
    # extracted action_type said. An action recorded all_in while this is False
    # is what P5-5 catches; the converse is deliberately not gated, since a
    # whole-stack call or raise is validly described either way.
    exhausts_stack: bool
    # What the seat has left afterwards. Negative beyond the tolerance means it
    # committed more than it held, which is a different defect from being
    # correctly all-in and needs its own branch in P5-5.
    chips_remaining_after: float


@dataclass
class StreetReplay:
    street_name: str
    actions: list[ActionReplay] = field(default_factory=list)
    largest_commitment: float = 0.0
    seats_still_in: tuple[str, ...] = ()
    seats_with_chips: tuple[str, ...] = ()
    hand_ended: bool = False
    betting_closed: bool = False

    @property
    def is_inert_boundary(self) -> bool:
        """D1: betting closed here with no further action possible, so every
        later street carries no decisions.

        Distinct from hand_ended, and the distinction is the whole rule. One
        seat left after folds means the hand is *over* — a later street is an
        error, not an inert skip.
        """
        return self.betting_closed and not self.hand_ended


@dataclass
class HandReplay:
    streets: list[StreetReplay] = field(default_factory=list)
    violation: Violation | None = None
    # Seats not folded when the replay ran out of actions. A winner must be one
    # of these.
    seats_still_in_at_end: tuple[str, ...] = ()
    # The street on which folds left a single seat, if any.
    hand_ended_on: str | None = None

    def street(self, street_name: str) -> StreetReplay | None:
        for s in self.streets:
            if s.street_name == street_name:
                return s
        return None

    def inert_streets(self) -> tuple[str, ...]:
        """Street names whose community cards carry no decisions.

        Everything after the first street where betting closed. Closure is
        monotone forward — once no seat can act, none can act later either — so
        these are always a suffix of STREET_ORDER, which is what keeps step E's
        prior-card accumulator consistent when they are skipped.

        The river is never inert: there is no street after it to be skipped.
        """
        for street in self.streets:
            if street.is_inert_boundary:
                index = STREET_ORDER.index(street.street_name)
                return STREET_ORDER[index + 1:]
        return ()


def _normalize(value: object) -> str:
    return str(value).strip().lower() if value is not None else ""


def posted_blind_for(seat_number: int, total_seat_count: int) -> float:
    """What this seat put in front of itself before the hand was dealt.

    By ROLE, never by label. normalize_heads_up rewrites the small blind to BTN
    before the row is written, so heads-up there is no seat labelled SB at all
    and a label lookup silently charges it nothing — wrong on every heads-up
    hand, which is where all-ins are most frequent.
    """
    if seat_number == 1:
        return BIG_BLIND
    if total_seat_count == 2:
        return SMALL_BLIND if seat_number == 3 else 0.0
    return SMALL_BLIND if seat_number == 2 else 0.0


def build_seats(hand_setup_state: dict, fva: dict) -> dict[str, Seat]:
    """The table as it stands the moment the FVA acts.

    Seats before the FVA in preflop acting order have already folded: preflop
    order is descending seat number, so those are the seats numbered *above* the
    FVA's. They have no action rows — step D is told to begin recording at the
    FVA — so they are folded from the start rather than folded by an action.
    """
    total_seat_count = hand_setup_state.get("total_seat_count")
    fva_seat_number = fva.get("seat_number")
    seats: dict[str, Seat] = {}
    for player in hand_setup_state.get("players", []):
        label = player.get("seat_position_label")
        seat_number = player.get("seat_number")
        blind = posted_blind_for(seat_number, total_seat_count)
        seats[label] = Seat(
            label=label,
            seat_number=seat_number,
            stack_size=float(player.get("stack_size") or 0.0),
            posted_blind=blind,
            street_blind=blind,
            folded=fva_seat_number is not None and seat_number > fva_seat_number,
            street_commitment=blind,
        )
    return seats


def interpret_all_in(largest_before: float, bet_amount: float) -> str:
    """What an extracted all_in actually is, against the running state.

    Nothing committed on the street yet -> a bet. At or below the current
    commitment -> a call, possibly for less than the full amount. Above it -> a
    raise. Preflop the largest starts at 1 (the BB's post), so this never yields
    a bet there, which matches the rule that a bet is never legal preflop.
    """
    if largest_before <= GATE_AMOUNT_TOLERANCE_BB:
        return "bet"
    if bet_amount <= largest_before + GATE_AMOUNT_TOLERANCE_BB:
        return "call"
    return "raise"


def _close_street(street: StreetReplay, seats: dict[str, Seat]) -> None:
    """Fill in a street's end state and evaluate D1 against it."""
    still_in = [s for s in seats.values() if not s.folded]
    with_chips = [s for s in still_in if not s.all_in]

    street.seats_still_in = tuple(s.label for s in still_in)
    street.seats_with_chips = tuple(s.label for s in with_chips)

    # D1 condition 1. Fewer than two seats means the hand ended by folds, which
    # is not closed betting — it is no hand at all.
    street.hand_ended = len(still_in) < 2
    if street.hand_ended:
        street.betting_closed = False
        return

    # D1 condition 2: everyone still in has matched the largest commitment, or
    # is all-in for less.
    matched = all(
        s.all_in or s.street_commitment >= street.largest_commitment - GATE_AMOUNT_TOLERANCE_BB
        for s in still_in
    )
    # D1 condition 3: at most one seat still in can put more chips in. Two seats
    # with chips can still contest a side pot, so the street is not closed.
    street.betting_closed = matched and len(with_chips) <= 1


def replay_hand(hand_setup_state: dict, fva: dict, streets: list[dict]) -> HandReplay:
    """Walk one hand's actions and return its running state.

    Stops at the first structural violation — an action by a seat that cannot
    act is not merely wrong, it makes everything after it meaningless, so
    continuing would report derived numbers nobody should trust.
    """
    seats = build_seats(hand_setup_state, fva)
    replay = HandReplay()

    by_name = {}
    for entry in streets or []:
        name = _normalize(entry.get("street_name"))
        if name not in STREET_ORDER:
            replay.violation = Violation(
                "unknown_street", f"step D reported street {entry.get('street_name')!r}"
            )
            return replay
        by_name[name] = entry.get("actions") or []

    hand_over = False
    for street_name in STREET_ORDER:
        if street_name not in by_name:
            continue

        if hand_over:
            replay.violation = Violation(
                "action_after_hand_end",
                f"step D reported {street_name} after the hand ended on "
                f"{replay.hand_ended_on}",
            )
            return replay

        street = StreetReplay(street_name=street_name)
        if street_name == "preflop":
            # The blinds are mandatory posts, not voluntary actions: nobody has
            # acted yet, but 1 BB is already owed.
            street.largest_commitment = BIG_BLIND
        else:
            for seat in seats.values():
                seat.committed_earlier = seat.committed_total
                seat.street_commitment = 0.0
                seat.street_blind = 0.0

        for raw in by_name[street_name]:
            label = raw.get("seat_position_label")
            seat = seats.get(label)
            if seat is None:
                replay.violation = Violation(
                    "action_label_unresolved",
                    f"{street_name} action {raw.get('action_order')} names seat "
                    f"{label!r}, which is not in this hand",
                )
                return replay

            if seat.folded or seat.all_in:
                why = "folded" if seat.folded else "all-in"
                replay.violation = Violation(
                    "action_after_fold_or_all_in",
                    f"{street_name} action {raw.get('action_order')}: {label} acts "
                    f"while {why}",
                )
                return replay

            action_type = _normalize(raw.get("action_type"))
            bet_amount = float(raw.get("bet_amount") or 0.0)
            largest_before = street.largest_commitment
            owed_before = max(largest_before - seat.street_commitment, 0.0)

            interpreted = (
                interpret_all_in(largest_before, bet_amount)
                if action_type == "all_in"
                else action_type
            )

            if action_type == "fold":
                seat.folded = True
            else:
                # Highest, not last. A fold's 0.0 must not erase what the seat
                # already put in this street.
                seat.street_commitment = max(seat.street_commitment, bet_amount)
                street.largest_commitment = max(street.largest_commitment, seat.street_commitment)

            street.actions.append(
                ActionReplay(
                    street_name=street_name,
                    action_order=raw.get("action_order"),
                    seat_label=label,
                    action_type=action_type,
                    bet_amount=bet_amount,
                    interpreted_type=interpreted,
                    largest_before=largest_before,
                    owed_before=owed_before,
                    exhausts_stack=seat.all_in,
                    chips_remaining_after=seat.chips_remaining,
                )
            )

        _close_street(street, seats)
        replay.streets.append(street)

        if street.hand_ended:
            hand_over = True
            replay.hand_ended_on = street_name

    replay.seats_still_in_at_end = tuple(s.label for s in seats.values() if not s.folded)
    return replay
