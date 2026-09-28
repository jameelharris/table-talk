SEAT_NUMBER_MAP = {
    "BB": 1, "SB": 2, "BTN": 3, "CO": 4,
    "HJ": 5, "LJ": 6, "UTG+2": 7, "UTG+1": 8, "UTG": 9,
}

# Seat number -> position label, as one ordered sequence. The inverse of
# SEAT_NUMBER_MAP, and the source of truth for which labels a table of a given
# size carries. extract_player_info.md assigns seat numbers by counting
# counter-clockwise from the BB, so an N-handed table occupies seats 1..N and
# uses exactly the first N of these.
#
# The consequence worth stating because it reads as a typo: a 7-handed table ends
# at UTG+2 and an 8-handed table at UTG+1. Neither has a UTG. UTG is seat 9 and
# exists only at a full ring.
SEAT_ORDER = ("BB", "SB", "BTN", "CO", "HJ", "LJ", "UTG+2", "UTG+1", "UTG")


def canonical_labels(total_seat_count: int) -> tuple[str, ...]:
    """The labels an N-handed table carries, in seat-number order (seat 1 first).

    Heads-up is the exception, and it is not a special case of the slice: the SB
    is the button, so normalize_heads_up rewrites SB -> BTN *before* the row is
    written. Stored heads-up rows therefore carry {BB, BTN} on seats {1, 3}, and
    seat 2 is never occupied. Returning ("BB", "SB") here would fail every real
    heads-up hand.
    """
    if total_seat_count == 2:
        return ("BB", "BTN")
    return SEAT_ORDER[:total_seat_count]


def preflop_acting_order(total_seat_count: int) -> tuple[str, ...]:
    """Preflop acting order: descending seat number, the BB last.

    Seat numbers count counter-clockwise from the BB, so the highest occupied
    seat acts first and seat 1 acts last. Heads-up this yields (BTN, BB), which
    is correct — the button posts the small blind and acts first preflop.
    """
    return tuple(reversed(canonical_labels(total_seat_count)))


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
