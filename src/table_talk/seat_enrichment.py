SEAT_NUMBER_MAP = {
    "BB": 1, "SB": 2, "BTN": 3, "CO": 4,
    "HJ": 5, "LJ": 6, "UTG+2": 7, "UTG+1": 8, "UTG": 9,
}


def add_seat_numbers(hand_setup_state: dict) -> dict:
    for player in hand_setup_state.get("players", []):
        player["seat_number"] = SEAT_NUMBER_MAP.get(player.get("seat_position_label"))
    hand_setup_state["players"].sort(key=lambda p: p.get("seat_number") or 999)
    return hand_setup_state


def add_fva_seat_number(fva_data: dict) -> dict:
    fva_data["seat_number"] = SEAT_NUMBER_MAP.get(fva_data.get("seat_position_label"))
    return fva_data


def heads_up_label(label: str | None, total_seat_count: int | None) -> str | None:
    """Heads-up, the SB is the BTN. Identity for every other label and seat count.

    Phase 5 needs this rule for bare position labels that live outside
    hand_setup_state — step D's actions[] and winning_positions[] — which is why
    it is a standalone function rather than a third optional parameter on
    normalize_heads_up.
    """
    if total_seat_count == 2 and label == "SB":
        return "BTN"
    return label


def _rewrite_seat(entry: dict, total_seat_count: int | None) -> None:
    """Apply heads_up_label to a dict carrying seat_position_label + seat_number."""
    label = heads_up_label(entry.get("seat_position_label"), total_seat_count)
    if label != entry.get("seat_position_label"):
        entry["seat_position_label"] = label
        entry["seat_number"] = SEAT_NUMBER_MAP[label]


def normalize_heads_up(hand_setup_state: dict, fva: dict | None = None) -> dict:
    total_seat_count = hand_setup_state.get("total_seat_count")
    if total_seat_count != 2:
        return hand_setup_state
    for player in hand_setup_state.get("players", []):
        _rewrite_seat(player, total_seat_count)
    if fva is not None:
        _rewrite_seat(fva, total_seat_count)
    return hand_setup_state
