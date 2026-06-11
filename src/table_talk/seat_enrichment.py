SEAT_NUMBER_MAP = {
    "BB": 1, "SB": 2, "BTN": 3, "CO": 4,
    "HJ": 5, "LJ": 6, "UTG+2": 7, "UTG+1": 8, "UTG": 9,
}


def add_seat_numbers(hand_setup_state: dict) -> dict:
    for player in hand_setup_state.get("players", []):
        player["seat_number"] = SEAT_NUMBER_MAP.get(player.get("seat_position_label"))
    hand_setup_state["players"].sort(key=lambda p: p.get("seat_number") or 999)
    return hand_setup_state


def normalize_heads_up(hand_setup_state: dict) -> dict:
    if hand_setup_state.get("total_seat_count") != 2:
        return hand_setup_state
    for player in hand_setup_state.get("players", []):
        if player.get("seat_position_label") == "SB":
            player["seat_position_label"] = "BTN"
            player["seat_number"] = SEAT_NUMBER_MAP["BTN"]
    return hand_setup_state
