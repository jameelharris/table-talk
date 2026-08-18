# These formats are a contract with the prompt files, which are written to expect exactly
# this text shape at their substitution slots. If you change a format here, the paired
# prompt file needs to change too.
#
#   build_player_context    -> {player_context}    in identify_hand_start.md
#   build_hole_card_context -> {hole_card_context} in extract_hole_cards.md
#   build_action_context    -> {player_context}    in extract_player_actions.md
#   build_fva_context       -> {fva_context}       in extract_player_actions.md
#
# build_fva_context had no caller between the step-C stack-anchor fix and Phase 5;
# extract_player_actions.md uses it again to establish that the FVA is action_order 1.

from table_talk.prompt_context import (
    build_action_context,
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
    assert build_hole_card_context(state) == "- Seat 1 (BB) | Stack: 100.0 BB"


def test_build_hole_card_context_nine_handed_fva_mid_table():
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": 5},
    }
    assert build_hole_card_context(state) == (
        "- Seat 1 (BB) | Stack: 100.0 BB\n"
        "- Seat 2 (SB) | Stack: 100.0 BB\n"
        "- Seat 3 (BTN) | Stack: 100.0 BB\n"
        "- Seat 4 (CO) | Stack: 100.0 BB\n"
        "- Seat 5 (HJ) | Stack: 100.0 BB"
    )


def test_build_hole_card_context_nine_handed_fva_at_last_seat():
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": 9},
    }
    result = build_hole_card_context(state)
    assert result.count("\n") == 8
    assert result.endswith("- Seat 9 (UTG) | Stack: 100.0 BB")


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
        "- Seat 1 (BB) | Stack: 100.0 BB\n"
        "- Seat 2 (SB) | Stack: 100.0 BB\n"
        "- Seat 3 (BTN) | Stack: 100.0 BB\n"
        "- Seat 4 (CO) | Stack: 100.0 BB"
    )


def test_build_hole_card_context_fva_seat_number_none_returns_all_unmarked():
    # add_fva_seat_number yields None for an unrecognized label; the filter
    # degrades to the full player list.
    state = {
        "hand_setup": {"players": _NINE_HANDED_PLAYERS},
        "fva": {"seat_number": None},
    }
    result = build_hole_card_context(state)
    assert result.count("\n") == 8
    assert result.count("| Stack: 100.0 BB") == 9


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
        "- Seat 1 (BB) | Stack: 100.0 BB\n"
        "- Seat 3 (BTN) | Stack: 100.0 BB"
    )


# ---------------------------------------------------------------------------
# build_action_context
# ---------------------------------------------------------------------------


def _action_state(players):
    return {"hand_setup": {"players": players}, "fva": {"seat_number": 9}}


def test_build_action_context_full_hole_cards():
    state = _action_state([
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5,
         "hole_cards": ["Ah", "Kd"]},
        {"seat_number": 9, "seat_position_label": "UTG", "stack_size": 22.0,
         "hole_cards": ["2c", "3c"]},
    ])
    assert build_action_context(state) == (
        "- Seat 1 (BB) | Stack: 14.5 BB | Hole cards: Ah Kd\n"
        "- Seat 9 (UTG) | Stack: 22.0 BB | Hole cards: 2c 3c"
    )


def test_build_action_context_null_hole_cards_render_unknown():
    state = _action_state([
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5, "hole_cards": None},
    ])
    assert build_action_context(state) == "- Seat 1 (BB) | Stack: 14.5 BB | Hole cards: unknown"


def test_build_action_context_partially_null_pair_renders_unknown():
    # Phase 4 can leave a half-read pair; half a hand is not a usable anchor.
    state = _action_state([
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5,
         "hole_cards": ["Ah", None]},
    ])
    assert build_action_context(state) == "- Seat 1 (BB) | Stack: 14.5 BB | Hole cards: unknown"


def test_build_action_context_missing_hole_cards_key_renders_unknown():
    state = _action_state([
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": 14.5},
    ])
    assert build_action_context(state) == "- Seat 1 (BB) | Stack: 14.5 BB | Hole cards: unknown"


def test_build_action_context_emits_all_seats_not_just_fva_eligible():
    # The contrast with build_hole_card_context: D tracks the hand to showdown,
    # so seats beyond the FVA must still appear.
    players = [dict(p, hole_cards=["Ah", "Kd"]) for p in _NINE_HANDED_PLAYERS]
    state = {"hand_setup": {"players": players}, "fva": {"seat_number": 3}}
    result = build_action_context(state)
    assert result.count("\n") == 8
    assert "- Seat 9 (UTG) | Stack: 100.0 BB | Hole cards: Ah Kd" in result


def test_build_action_context_empty_player_list():
    assert build_action_context(_action_state([])) == ""


def test_build_action_context_null_stack_renders_none():
    # check_preconditions does not re-check stacks in Phase 5 (Phase 4 already
    # skipped any hand with a null stack), so this is a shape guarantee only.
    state = _action_state([
        {"seat_number": 1, "seat_position_label": "BB", "stack_size": None,
         "hole_cards": ["Ah", "Kd"]},
    ])
    assert build_action_context(state) == "- Seat 1 (BB) | Stack: None BB | Hole cards: Ah Kd"
