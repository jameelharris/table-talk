# These formats are a contract with the PR 7 prompt files (prompts/identify_hand_start.md,
# prompts/extract_hole_cards.md), which are written to expect exactly this text shape at
# their {player_context}/{fva_context}/{hole_card_context} slots. If you change a format
# here, the paired prompt file needs to change too.

from table_talk.prompt_context import (
    build_fva_context,
    build_hole_card_context,
    build_player_context,
)

_NINE_HANDED_PLAYERS = [
    {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0},
    {"seat_number": 2, "seat_position_label": "SB", "stack_size": 100.0},
    {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 100.0},
    {"seat_number": 4, "seat_position_label": "CO", "stack_size": 100.0},
    {"seat_number": 5, "seat_position_label": "HJ", "stack_size": 100.0},
    {"seat_number": 6, "seat_position_label": "LJ", "stack_size": 100.0},
    {"seat_number": 7, "seat_position_label": "UTG+2", "stack_size": 100.0},
    {"seat_number": 8, "seat_position_label": "UTG+1", "stack_size": 100.0},
    {"seat_number": 9, "seat_position_label": "UTG", "stack_size": 100.0},
]


# ---------------------------------------------------------------------------
# build_player_context
# ---------------------------------------------------------------------------


def test_build_player_context_single_player():
    state = {"players": [{"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5}]}
    assert build_player_context(state) == "- Seat 1 (BB) | Stack: 14.5 BB"


def test_build_player_context_multiple_players():
    state = {
        "players": [
            {"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5},
            {"seat_number": 2, "seat_position_label": "SB", "stack_size": 22.0},
        ]
    }
    assert build_player_context(state) == (
        "- Seat 1 (BB) | Stack: 14.5 BB\n"
        "- Seat 2 (SB) | Stack: 22.0 BB"
    )


# ---------------------------------------------------------------------------
# build_fva_context
# ---------------------------------------------------------------------------


def test_build_fva_context():
    fva = {"seat_number": 9, "seat_position_label": "UTG", "action_type": "all_in", "bet_amount": 5.98}
    assert build_fva_context(fva) == "Seat 9 (UTG)\nAction: all_in 5.98 BB"


# ---------------------------------------------------------------------------
# build_hole_card_context
# ---------------------------------------------------------------------------


def test_build_hole_card_context_nine_handed_fva_at_first_seat():
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": 1},
    }
    assert build_hole_card_context(state) == "- Seat 1 (BB) — FVA, stop here"


def test_build_hole_card_context_nine_handed_fva_mid_table():
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": 5},
    }
    assert build_hole_card_context(state) == (
        "- Seat 1 (BB)\n"
        "- Seat 2 (SB)\n"
        "- Seat 3 (BTN)\n"
        "- Seat 4 (CO)\n"
        "- Seat 5 (HJ) — FVA, stop here"
    )


def test_build_hole_card_context_nine_handed_fva_at_last_seat():
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": 9},
    }
    result = build_hole_card_context(state)
    assert result.count("\n") == 8
    assert result.endswith("- Seat 9 (UTG) — FVA, stop here")


def test_build_hole_card_context_six_handed():
    players = [
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0},
        {"seat_number": 2, "seat_position_label": "SB", "stack_size": 100.0},
        {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 100.0},
        {"seat_number": 4, "seat_position_label": "CO", "stack_size": 100.0},
        {"seat_number": 5, "seat_position_label": "HJ", "stack_size": 100.0},
        {"seat_number": 6, "seat_position_label": "LJ", "stack_size": 100.0},
    ]
    state = {
        "hand_setup": {"players": players},
        "fva": {"seat_number": 4},
    }
    assert build_hole_card_context(state) == (
        "- Seat 1 (BB)\n"
        "- Seat 2 (SB)\n"
        "- Seat 3 (BTN)\n"
        "- Seat 4 (CO) — FVA, stop here"
    )


def test_build_hole_card_context_fva_seat_number_none_returns_all_unmarked():
    # add_fva_seat_number yields None for an unrecognized label; the filter
    # degrades to the full player list with no FVA marker.
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": None},
    }
    result = build_hole_card_context(state)
    assert result.count("\n") == 8
    assert "FVA, stop here" not in result


def test_build_hole_card_context_heads_up():
    # After seat_enrichment.normalize_heads_up runs upstream, SB is relabeled BTN (seat 3).
    players = [
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 100.0},
        {"seat_number": 3, "seat_position_label": "BTN", "stack_size": 100.0},
    ]
    state = {
        "hand_setup": {"players": players},
        "fva": {"seat_number": 3},
    }
    assert build_hole_card_context(state) == (
        "- Seat 1 (BB)\n"
        "- Seat 3 (BTN) — FVA, stop here"
    )
