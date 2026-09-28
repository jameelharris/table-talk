import pytest

from table_talk.seat_enrichment import (
    SEAT_NUMBER_MAP,
    SEAT_ORDER,
    add_fva_seat_number,
    add_seat_numbers,
    canonical_labels,
    heads_up_label,
    normalize_heads_up,
    preflop_acting_order,
)


def _make_state(labels, total_seat_count=9):
    return {
        "total_seat_count": total_seat_count,
        "players": [{"seat_position_label": lbl, "stack_size": 100.0} for lbl in labels],
    }


def test_add_seat_numbers_9_handed():
    all_labels = ["BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1", "UTG"]
    state = _make_state(all_labels, total_seat_count=9)
    add_seat_numbers(state)

    assigned = {p["seat_position_label"]: p["seat_number"] for p in state["players"]}
    assert assigned == SEAT_NUMBER_MAP

    seat_numbers = [p["seat_number"] for p in state["players"]]
    assert seat_numbers == sorted(seat_numbers)


def test_add_seat_numbers_6_handed():
    labels = ["HJ", "BTN", "BB", "SB", "LJ", "CO"]
    state = _make_state(labels, total_seat_count=6)
    add_seat_numbers(state)

    assigned = {p["seat_position_label"]: p["seat_number"] for p in state["players"]}
    assert assigned == {"BB": 1, "SB": 2, "BTN": 3, "CO": 4, "HJ": 5, "LJ": 6}

    seat_numbers = [p["seat_number"] for p in state["players"]]
    assert seat_numbers == [1, 2, 3, 4, 5, 6]


def test_add_seat_numbers_carries_bounty_through_untouched():
    """Phase 3 writes `bounty` onto player objects before enrichment runs.
    Enrichment injects seat_number and sorts; it must not drop or alter any
    other field it does not know about."""
    state = {
        "total_seat_count": 3,
        "players": [
            {"seat_position_label": "UTG", "stack_size": 50.0, "bounty": 281.25},
            {"seat_position_label": "BB", "stack_size": 100.0, "bounty": 125.0},
            {"seat_position_label": "SB", "stack_size": 75.0, "bounty": None},
        ],
    }
    add_seat_numbers(state)

    by_label = {p["seat_position_label"]: p for p in state["players"]}
    assert by_label["UTG"]["bounty"] == 281.25
    assert by_label["BB"]["bounty"] == 125.0
    assert by_label["SB"]["bounty"] is None
    # Sorting moved the players; the bounty must have travelled with its seat.
    assert state["players"][0]["seat_position_label"] == "BB"
    assert state["players"][0]["bounty"] == 125.0


def test_add_seat_numbers_unknown_label_yields_none():
    state = _make_state(["BB", "WEIRD", "BTN"])
    add_seat_numbers(state)

    labels_in_order = [p["seat_position_label"] for p in state["players"]]
    # BB=1 and BTN=3 come before WEIRD (None → 999)
    assert labels_in_order[-1] == "WEIRD"
    weird_player = next(p for p in state["players"] if p["seat_position_label"] == "WEIRD")
    assert weird_player["seat_number"] is None


def test_add_seat_numbers_idempotent():
    state = _make_state(["CO", "BB", "SB"])
    add_seat_numbers(state)
    first = [p["seat_number"] for p in state["players"]]
    add_seat_numbers(state)
    second = [p["seat_number"] for p in state["players"]]
    assert first == second


def test_normalize_heads_up_rewrites_sb_to_btn():
    state = {
        "total_seat_count": 2,
        "players": [
            {"seat_position_label": "BB", "seat_number": 1, "stack_size": 100.0},
            {"seat_position_label": "SB", "seat_number": 2, "stack_size": 100.0},
        ],
    }
    normalize_heads_up(state)

    labels = {p["seat_position_label"] for p in state["players"]}
    assert "SB" not in labels
    btn = next(p for p in state["players"] if p["seat_position_label"] == "BTN")
    assert btn["seat_number"] == 3


def test_normalize_heads_up_noop_for_non_heads_up():
    state = {
        "total_seat_count": 9,
        "players": [
            {"seat_position_label": "SB", "seat_number": 2, "stack_size": 100.0},
        ],
    }
    normalize_heads_up(state)
    assert state["players"][0]["seat_position_label"] == "SB"
    assert state["players"][0]["seat_number"] == 2


def test_normalize_heads_up_no_sb_present():
    state = {
        "total_seat_count": 2,
        "players": [
            {"seat_position_label": "BB", "seat_number": 1, "stack_size": 100.0},
            {"seat_position_label": "BTN", "seat_number": 3, "stack_size": 100.0},
        ],
    }
    before = [dict(p) for p in state["players"]]
    normalize_heads_up(state)
    assert state["players"] == before


def test_add_fva_seat_number_known_label():
    fva = {"seat_position_label": "UTG", "action_type": "raise", "bet_amount": 3.0}
    add_fva_seat_number(fva)
    assert fva["seat_number"] == SEAT_NUMBER_MAP["UTG"]


def test_add_fva_seat_number_unknown_label():
    fva = {"seat_position_label": "WEIRD", "action_type": "raise", "bet_amount": 3.0}
    add_fva_seat_number(fva)
    assert fva["seat_number"] is None


def test_normalize_heads_up_with_fva_sb_rewrites_label_and_seat_number():
    state = {
        "total_seat_count": 2,
        "players": [
            {"seat_position_label": "BB", "seat_number": 1, "stack_size": 100.0},
            {"seat_position_label": "SB", "seat_number": 2, "stack_size": 100.0},
        ],
    }
    fva = {"seat_position_label": "SB", "seat_number": 2, "action_type": "raise", "bet_amount": 3.0}

    normalize_heads_up(state, fva=fva)

    assert fva["seat_position_label"] == "BTN"
    assert fva["seat_number"] == SEAT_NUMBER_MAP["BTN"]
    # players[] rewrite still happens alongside the fva rewrite
    labels = {p["seat_position_label"] for p in state["players"]}
    assert "SB" not in labels


def test_normalize_heads_up_with_fva_non_sb_unchanged():
    state = {
        "total_seat_count": 2,
        "players": [
            {"seat_position_label": "BB", "seat_number": 1, "stack_size": 100.0},
            {"seat_position_label": "SB", "seat_number": 2, "stack_size": 100.0},
        ],
    }
    fva = {"seat_position_label": "BB", "seat_number": 1, "action_type": "call", "bet_amount": None}
    before = dict(fva)

    normalize_heads_up(state, fva=fva)

    assert fva == before


def test_normalize_heads_up_non_heads_up_with_fva_unchanged():
    state = {
        "total_seat_count": 9,
        "players": [
            {"seat_position_label": "SB", "seat_number": 2, "stack_size": 100.0},
        ],
    }
    fva = {"seat_position_label": "SB", "seat_number": 2, "action_type": "raise", "bet_amount": 3.0}
    before = dict(fva)

    normalize_heads_up(state, fva=fva)

    assert fva == before


# heads_up_label is the pure atom normalize_heads_up rewrites through. Phase 5
# applies it directly to bare labels in step D's actions[] and winning_positions[],
# which do not live inside hand_setup_state.


def test_heads_up_label_sb_heads_up_becomes_btn():
    assert heads_up_label("SB", 2) == "BTN"


def test_heads_up_label_sb_not_heads_up_unchanged():
    assert heads_up_label("SB", 6) == "SB"
    assert heads_up_label("SB", 9) == "SB"


def test_heads_up_label_non_sb_unchanged_heads_up():
    assert heads_up_label("BB", 2) == "BB"
    assert heads_up_label("BTN", 2) == "BTN"


def test_heads_up_label_none_label_unchanged():
    assert heads_up_label(None, 2) is None


def test_heads_up_label_none_seat_count_unchanged():
    assert heads_up_label("SB", None) == "SB"


# ---------------------------------------------------------------------------
# Canonical labels per table size
#
# P4-3 gates on these, and the Phase 6 positions seed will assert against them,
# so the two cannot drift.
# ---------------------------------------------------------------------------


def test_seat_order_agrees_with_seat_number_map():
    """Two constants encoding one fact. This is what stops them diverging."""
    assert {label: i + 1 for i, label in enumerate(SEAT_ORDER)} == SEAT_NUMBER_MAP


@pytest.mark.parametrize(
    "size,expected",
    [
        (2, ("BB", "BTN")),
        (3, ("BB", "SB", "BTN")),
        (4, ("BB", "SB", "BTN", "CO")),
        (5, ("BB", "SB", "BTN", "CO", "HJ")),
        (6, ("BB", "SB", "BTN", "CO", "HJ", "LJ")),
        (7, ("BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2")),
        (8, ("BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1")),
        (9, ("BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1", "UTG")),
    ],
)
def test_canonical_labels_for_every_table_size(size, expected):
    assert canonical_labels(size) == expected


def test_seven_and_eight_handed_have_no_utg():
    """Reads as a typo and is not: UTG is seat 9 and exists only at a full ring.
    A short table's last seat is UTG+2 or UTG+1."""
    assert "UTG" not in canonical_labels(7)
    assert "UTG" not in canonical_labels(8)
    assert "UTG" in canonical_labels(9)


def test_heads_up_labels_are_post_normalization():
    """normalize_heads_up runs in Phase 3 before the row is written, so a stored
    heads-up hand carries BTN and never SB. Returning ("BB", "SB") here would
    fail P4-3 on every real heads-up hand."""
    assert canonical_labels(2) == ("BB", "BTN")
    assert "SB" not in canonical_labels(2)


def test_canonical_labels_agree_with_what_add_seat_numbers_assigns():
    """The labels a size-N table carries are exactly the ones whose seat numbers
    fall in 1..N under the map Phase 3 applies."""
    for size in range(3, 10):
        state = {
            "total_seat_count": size,
            "players": [{"seat_position_label": label} for label in canonical_labels(size)],
        }
        add_seat_numbers(state)
        assert sorted(p["seat_number"] for p in state["players"]) == list(range(1, size + 1))


@pytest.mark.parametrize(
    "size,first,last",
    [(9, "UTG", "BB"), (6, "LJ", "BB"), (3, "BTN", "BB"), (2, "BTN", "BB")],
)
def test_preflop_acting_order_runs_high_seat_to_bb(size, first, last):
    order = preflop_acting_order(size)
    assert order[0] == first
    assert order[-1] == last
    assert set(order) == set(canonical_labels(size))


def test_heads_up_button_acts_first_preflop():
    """The button posts the small blind heads-up and acts first preflop."""
    assert preflop_acting_order(2) == ("BTN", "BB")
