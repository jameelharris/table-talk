from table_talk.seat_enrichment import (
    SEAT_NUMBER_MAP,
    add_fva_seat_number,
    add_seat_numbers,
    normalize_heads_up,
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
