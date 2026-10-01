import pytest

from table_talk.betting_state import (
    GATE_AMOUNT_TOLERANCE_BB,
    build_seats,
    interpret_all_in,
    posted_blind_for,
    replay_hand,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LABELS_BY_SIZE = {
    2: ["BB", "BTN"],
    3: ["BB", "SB", "BTN"],
    4: ["BB", "SB", "BTN", "CO"],
    5: ["BB", "SB", "BTN", "CO", "HJ"],
}
_SEAT_NUMBERS = {"BB": 1, "SB": 2, "BTN": 3, "CO": 4, "HJ": 5}


def _setup(stacks: dict[str, float]):
    """A hand setup whose seats are exactly the canonical set for its size."""
    size = len(stacks)
    labels = _LABELS_BY_SIZE[size]
    assert set(stacks) == set(labels), f"{sorted(stacks)} is not a valid {size}-handed table"
    return {
        "total_seat_count": size,
        "pot_size_bb": 1.5,
        "players": [
            {
                "seat_position_label": label,
                "seat_number": _SEAT_NUMBERS[label],
                "stack_size": stacks[label],
            }
            for label in labels
        ],
    }


def _fva(label, action_type="raise", bet_amount=2.5):
    return {
        "seat_position_label": label,
        "seat_number": _SEAT_NUMBERS[label],
        "action_type": action_type,
        "bet_amount": bet_amount,
    }


def _street(name, *actions):
    return {
        "street_name": name,
        "actions": [
            {
                "action_order": i,
                "seat_position_label": label,
                "action_type": action_type,
                "bet_amount": amount,
            }
            for i, (label, action_type, amount) in enumerate(actions, start=1)
        ],
    }


# ---------------------------------------------------------------------------
# D1 — the inert-street rule, every row of the table
# ---------------------------------------------------------------------------


def test_d1_preflop_all_in_and_call_makes_every_later_street_inert():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        # 20.5 in front, not 20.0: heads-up the BTN posts the small blind, and
        # stack_size is already net of it.
        [_street("preflop", ("BTN", "all_in", 20.5), ("BB", "call", 20.5))],
    )
    assert replay.violation is None
    assert replay.inert_streets() == ("flop", "turn", "river")


def test_d1_flop_all_in_and_call_makes_turn_and_river_inert():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
            # Postflop carries no blind: BTN put 2.0 of its 20.0 in preflop, so
            # 18.0 is the rest of the stack.
            _street("flop", ("BB", "check", 0.0), ("BTN", "all_in", 18.0), ("BB", "call", 18.0)),
        ],
    )
    assert replay.violation is None
    assert replay.inert_streets() == ("turn", "river")


def test_d1_turn_all_in_and_call_makes_only_the_river_inert():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
            _street("turn", ("BTN", "all_in", 18.0), ("BB", "call", 18.0)),
        ],
    )
    assert replay.violation is None
    assert replay.inert_streets() == ("river",)


def test_d1_river_all_in_and_call_leaves_nothing_to_skip():
    """The river is never inert: there is no street after it."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
            _street("turn", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
            _street("river", ("BTN", "all_in", 18.0), ("BB", "call", 18.0)),
        ],
    )
    assert replay.violation is None
    assert replay.inert_streets() == ()


def test_d1_not_closed_when_a_seat_has_not_yet_acted():
    """Condition 2. The BB still owes and has not answered the all-in."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0, "CO": 15.0}),
        _fva("CO", "all_in", 15.0),
        [_street("preflop", ("CO", "all_in", 15.0), ("BTN", "fold", 0.0), ("SB", "fold", 0.0))],
    )
    assert replay.violation is None
    assert replay.inert_streets() == ()
    assert replay.street("preflop").betting_closed is False


def test_d1_not_closed_when_two_larger_stacks_can_still_contest_a_side_pot():
    """Condition 3. A short stack all-in called by two deeper seats leaves two
    seats with chips, who can go on betting into a side pot."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 5.0}),
        _fva("BTN", "all_in", 5.0),
        [_street("preflop", ("BTN", "all_in", 5.0), ("SB", "call", 5.0), ("BB", "call", 5.0))],
    )
    assert replay.violation is None
    preflop = replay.street("preflop")
    assert sorted(preflop.seats_with_chips) == ["BB", "SB"]
    assert preflop.betting_closed is False
    assert replay.inert_streets() == ()


def test_d1_closed_when_the_third_seat_folded_earlier():
    """The same shape as above, but the third seat is gone — one seat with
    chips remains, so the turn and river carry no decisions."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 5.0}),
        _fva("BTN", "raise", 2.0),
        [
            _street("preflop", ("BTN", "raise", 2.0), ("SB", "fold", 0.0), ("BB", "call", 2.0)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "all_in", 3.0), ("BB", "call", 3.0)),
        ],
    )
    assert replay.violation is None
    assert replay.street("flop").seats_with_chips == ("BB",)
    assert replay.inert_streets() == ("turn", "river")


def test_one_seat_left_after_folds_is_the_hand_ending_not_closed_betting():
    """The distinction the whole rule turns on. A street after this point is an
    error, never an inert skip."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        [_street("preflop", ("BTN", "all_in", 20.5), ("BB", "fold", 0.0))],
    )
    assert replay.violation is None
    preflop = replay.street("preflop")
    assert preflop.hand_ended is True
    assert preflop.betting_closed is False
    assert preflop.is_inert_boundary is False
    assert replay.inert_streets() == ()
    assert replay.hand_ended_on == "preflop"


def test_a_street_reported_after_the_hand_ended_is_a_violation():
    """The documented over-report: a hand that ended with a fold on the turn,
    with a river reported anyway."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
            _street("turn", ("BB", "bet", 4.0), ("BTN", "fold", 0.0)),
            _street("river", ("BB", "check", 0.0)),
        ],
    )
    assert replay.violation is not None
    assert replay.violation.code == "action_after_hand_end"
    assert "river" in replay.violation.detail


# ---------------------------------------------------------------------------
# D2 — the conventions, each one on its own
# ---------------------------------------------------------------------------


def test_blinds_are_identified_by_role_not_by_label():
    """Heads-up, normalize_heads_up has already rewritten the small blind to
    BTN, so no seat is labelled SB. A label lookup charges it nothing and gets
    every heads-up all-in wrong."""
    assert posted_blind_for(seat_number=1, total_seat_count=2) == 1.0
    assert posted_blind_for(seat_number=3, total_seat_count=2) == 0.5   # BTN is the SB
    assert posted_blind_for(seat_number=2, total_seat_count=2) == 0.0   # seat 2 is unused

    assert posted_blind_for(seat_number=1, total_seat_count=9) == 1.0
    assert posted_blind_for(seat_number=2, total_seat_count=9) == 0.5
    assert posted_blind_for(seat_number=3, total_seat_count=9) == 0.0   # BTN is not a blind


def test_heads_up_button_all_in_threshold_is_stack_plus_the_small_blind():
    """The dedicated heads-up case. A BTN showing 20.0 is all-in at 20.5."""
    seats = build_seats(_setup({"BB": 40.0, "BTN": 20.0}), _fva("BTN", "all_in", 20.5))
    btn = seats["BTN"]
    assert btn.posted_blind == 0.5
    btn.street_commitment = 20.5
    assert btn.all_in is True
    assert btn.chips_remaining == pytest.approx(0.0)

    # One tenth short of the stack is not all-in.
    btn.street_commitment = 20.0
    assert btn.all_in is False


def test_stacks_are_net_of_posts_so_the_blind_is_not_double_counted():
    seats = build_seats(_setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}), _fva("BTN"))
    # Nothing voluntary has happened, so nobody has spent anything yet.
    assert seats["BB"].committed_total == pytest.approx(0.0)
    assert seats["SB"].committed_total == pytest.approx(0.0)
    assert seats["BB"].chips_remaining == pytest.approx(40.0)
    assert seats["SB"].chips_remaining == pytest.approx(30.0)


def test_the_bb_ante_never_enters_the_arithmetic():
    """The whole table's ante is posted by the BB and does not sit in front of
    it, so it is already netted out of stack_size. Subtracting it per seat is a
    documented way to get this wrong."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        [_street("preflop", ("BTN", "all_in", 20.5), ("BB", "call", 20.5))],
    )
    action = replay.street("preflop").actions[1]
    assert action.seat_label == "BB"
    # 20.5 in front, 1.0 of it the posted blind -> 19.5 from a 40.0 stack.
    assert action.chips_remaining_after == pytest.approx(20.5)


def test_street_commitment_is_the_highest_not_the_last():
    """A seat that bets 3 and folds to a raise committed 3. Its fold carries
    bet_amount 0.0, which must not erase that."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("SB", "call", 2.5), ("BB", "call", 2.5)),
            _street(
                "flop",
                ("SB", "bet", 3.0),
                ("BB", "raise", 9.0),
                ("SB", "fold", 0.0),
                ("BTN", "fold", 0.0),
            ),
        ],
    )
    assert replay.violation is None
    # SB spent 2.0 preflop (2.5 less its 0.5 blind) plus the 3.0 it bet and lost.
    sb_fold = replay.street("flop").actions[2]
    assert sb_fold.seat_label == "SB"
    assert sb_fold.chips_remaining_after == pytest.approx(30.0 - 2.0 - 3.0)


def test_pre_fva_folders_are_folded_from_the_start_with_no_action_rows():
    """Preflop order is descending seat number, so seats numbered above the FVA
    have already folded. Step D is told to begin at the FVA, so they never
    appear in the sequence."""
    seats = build_seats(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0, "CO": 15.0, "HJ": 12.0}),
        _fva("BTN", "raise", 2.5),
    )
    assert seats["HJ"].folded is True   # seat 5, acts before the BTN
    assert seats["CO"].folded is True   # seat 4, acts before the BTN
    assert seats["BTN"].folded is False
    assert seats["SB"].folded is False
    assert seats["BB"].folded is False


def test_a_blind_all_in_from_its_post_is_in_the_hand_but_cannot_act():
    """Zero behind after posting. Not observed anywhere in the corpus — the
    shortest blind seen still had 1.58 BB behind — so this branch rests on
    reasoning rather than on data.

    Getting it wrong is not harmless: counted as holding chips, the seat keeps
    D1's condition 3 from ever being satisfied and a genuinely closed street
    reads as live.
    """
    seats = build_seats(_setup({"BB": 0.0, "BTN": 20.0}), _fva("BTN", "all_in", 20.5))
    bb = seats["BB"]
    assert bb.folded is False      # still in the hand
    assert bb.all_in is True       # but cannot put another chip in
    assert bb.can_act is False


def test_a_blind_all_in_from_its_post_lets_the_street_close():
    replay = replay_hand(
        _setup({"BB": 0.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        [_street("preflop", ("BTN", "all_in", 20.5))],
    )
    assert replay.violation is None
    preflop = replay.street("preflop")
    assert sorted(preflop.seats_still_in) == ["BB", "BTN"]
    assert preflop.seats_with_chips == ()
    assert replay.inert_streets() == ("flop", "turn", "river")


def test_an_action_by_a_seat_that_is_all_in_is_a_violation():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        [
            _street("preflop", ("BTN", "all_in", 20.5), ("BB", "call", 20.5)),
            _street("flop", ("BTN", "bet", 5.0)),
        ],
    )
    assert replay.violation.code == "action_after_fold_or_all_in"
    assert "BTN" in replay.violation.detail


def test_an_action_by_a_seat_not_in_the_hand_is_a_violation():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [_street("preflop", ("BTN", "raise", 2.5), ("CO", "call", 2.5))],
    )
    assert replay.violation.code == "action_label_unresolved"
    assert "CO" in replay.violation.detail


def test_preflop_owes_one_big_blind_before_anyone_acts():
    """The blinds are mandatory posts, not voluntary actions, but 1 BB is owed
    from the start — which is why a preflop all_in is never interpreted as a
    bet."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "call", 1.0),
        [_street("preflop", ("BTN", "call", 1.0), ("SB", "fold", 0.0), ("BB", "check", 0.0))],
    )
    first = replay.street("preflop").actions[0]
    assert first.largest_before == 1.0
    assert first.owed_before == 1.0


def test_the_bb_owes_nothing_preflop_and_may_check():
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "call", 1.0),
        [_street("preflop", ("BTN", "call", 1.0), ("SB", "fold", 0.0), ("BB", "check", 0.0))],
    )
    bb_check = replay.street("preflop").actions[2]
    assert bb_check.seat_label == "BB"
    assert bb_check.owed_before == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Interpreting an extracted all_in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("largest,amount,expected", [
    (0.0, 5.0, "bet"),        # nothing committed on the street yet
    (4.0, 4.0, "call"),       # matches exactly
    (4.0, 2.0, "call"),       # all-in for less than the current bet
    (4.0, 9.0, "raise"),      # exceeds it
    (1.0, 12.0, "raise"),     # preflop, over the BB
])
def test_interpret_all_in(largest, amount, expected):
    assert interpret_all_in(largest, amount) == expected


def test_a_preflop_all_in_is_never_interpreted_as_a_bet():
    """A bet is never legal preflop: the BB's post means something is always
    owed."""
    assert interpret_all_in(1.0, 20.5) != "bet"


def test_the_stored_action_type_is_never_rewritten():
    """An extracted all_in is interpreted for the checks only."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "all_in", 20.5),
        [_street("preflop", ("BTN", "all_in", 20.5), ("BB", "call", 20.5))],
    )
    action = replay.street("preflop").actions[0]
    assert action.action_type == "all_in"        # as extracted
    assert action.interpreted_type == "raise"    # as understood


# ---------------------------------------------------------------------------
# The two real corpus hands from ARCHITECTURE
# ---------------------------------------------------------------------------


def test_t772_sb_showing_58_is_all_in_at_58_point_5():
    """The broadcast at t=772: an SB with a displayed stack of 58 moves all-in
    and the chips in front read 58.5. This is the case that settled the
    blind-inclusive convention."""
    replay = replay_hand(
        _setup({"BB": 90.0, "SB": 58.0, "BTN": 40.0}),
        _fva("SB", "all_in", 58.5),
        [_street("preflop", ("SB", "all_in", 58.5), ("BB", "fold", 0.0))],
    )
    assert replay.violation is None
    action = replay.street("preflop").actions[0]
    assert action.exhausts_stack is True
    assert action.chips_remaining_after == pytest.approx(0.0)


def test_t584_sb_showing_11_1_raises_to_7_then_shoves_4_55_on_the_flop():
    """The rounding case. 7 preflop leaves 4.6 behind; the screen reads 4.55 on
    the shove. It reconciles only under the blind-inclusive reading, and only
    within the tolerance."""
    replay = replay_hand(
        _setup({"BB": 20.0, "SB": 11.1, "BTN": 15.0}),
        _fva("SB", "raise", 7.0),
        [
            _street("preflop", ("SB", "raise", 7.0), ("BB", "call", 7.0)),
            _street("flop", ("SB", "all_in", 4.55), ("BB", "call", 4.55)),
        ],
    )
    assert replay.violation is None
    preflop_raise = replay.street("preflop").actions[0]
    assert preflop_raise.exhausts_stack is False
    assert preflop_raise.chips_remaining_after == pytest.approx(4.6)

    shove = replay.street("flop").actions[0]
    assert shove.exhausts_stack is True
    # 0.05 short of exact, inside the 0.1 tolerance and outside a 0.01 one.
    assert shove.chips_remaining_after == pytest.approx(0.05)
    assert abs(shove.chips_remaining_after) <= GATE_AMOUNT_TOLERANCE_BB


def test_a_transposed_raise_and_shove_does_not_exhaust_the_stack():
    """The same hand with the pair swapped: 4.55 preflop and 6.55 on the flop.
    The flop action is recorded as an all_in but leaves chips behind, which is
    what P5-5's forward branch catches — and, since P5-5 no longer checks the
    reverse direction, the only branch that catches a transposed pair."""
    replay = replay_hand(
        _setup({"BB": 20.0, "SB": 11.1, "BTN": 15.0}),
        _fva("SB", "raise", 4.55),
        [
            _street("preflop", ("SB", "raise", 4.55), ("BB", "call", 4.55)),
            _street("flop", ("SB", "all_in", 6.55), ("BB", "call", 6.55)),
        ],
    )
    assert replay.violation is None
    shove = replay.street("flop").actions[0]
    assert shove.action_type == "all_in"
    assert shove.exhausts_stack is False          # but it is not actually all-in
    assert shove.chips_remaining_after == pytest.approx(0.5)


def test_committing_more_than_the_stack_leaves_a_negative_remainder():
    """A raise to 70 with 58 behind. Distinct from being correctly all-in, and
    it needs its own branch in P5-5."""
    replay = replay_hand(
        _setup({"BB": 90.0, "SB": 58.0, "BTN": 40.0}),
        _fva("SB", "raise", 70.0),
        [_street("preflop", ("SB", "raise", 70.0), ("BB", "fold", 0.0))],
    )
    action = replay.street("preflop").actions[0]
    assert action.chips_remaining_after == pytest.approx(-11.5)
    assert action.chips_remaining_after < -GATE_AMOUNT_TOLERANCE_BB


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_an_unrecognised_street_name_is_a_violation_not_a_silent_drop():
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [_street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)), _street("turn2")],
    )
    assert replay.violation.code == "unknown_street"


def test_streets_are_replayed_in_canonical_order_not_the_order_d_returned():
    """A mis-ordered response must not make the turn's state depend on the
    river's."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("flop", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
        ],
    )
    assert replay.violation is None
    assert [s.street_name for s in replay.streets] == ["preflop", "flop"]


def test_seats_still_in_at_end_is_what_a_winner_must_come_from():
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("SB", "fold", 0.0), ("BB", "call", 2.5)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "check", 0.0)),
        ],
    )
    assert sorted(replay.seats_still_in_at_end) == ["BB", "BTN"]


def test_a_hand_ending_in_folds_leaves_exactly_the_one_winner():
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [_street("preflop", ("BTN", "raise", 2.5), ("SB", "fold", 0.0), ("BB", "fold", 0.0))],
    )
    assert replay.seats_still_in_at_end == ("BTN",)
    assert replay.hand_ended_on == "preflop"


def test_inert_streets_are_always_a_suffix():
    """Closure is monotone forward — once no seat can act, none can act later —
    which is what keeps step E's prior-card accumulator consistent when the
    inert streets are skipped."""
    replay = replay_hand(
        _setup({"BB": 40.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [
            _street("preflop", ("BTN", "raise", 2.5), ("BB", "call", 2.5)),
            _street("flop", ("BB", "check", 0.0), ("BTN", "all_in", 18.0), ("BB", "call", 18.0)),
            _street("turn"),
            _street("river"),
        ],
    )
    inert = replay.inert_streets()
    from table_talk.betting_state import STREET_ORDER
    assert inert == STREET_ORDER[STREET_ORDER.index("turn"):]


def test_a_hand_that_ends_preflop_by_folds_has_no_inert_streets():
    """Nothing to skip, because nothing should follow. Contrast with a called
    all-in, where the later streets exist and are inert."""
    replay = replay_hand(
        _setup({"BB": 40.0, "SB": 30.0, "BTN": 20.0}),
        _fva("BTN", "raise", 2.5),
        [_street("preflop", ("BTN", "raise", 2.5), ("SB", "fold", 0.0), ("BB", "fold", 0.0))],
    )
    assert replay.inert_streets() == ()
