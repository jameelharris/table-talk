def build_player_context(hand_setup_state: dict) -> str:
    lines = []
    for p in hand_setup_state.get("players", []):
        lines.append(
            f"- Seat {p.get('seat_number')} ({p.get('seat_position_label')}) | Stack: {p.get('stack_size')} BB"
        )
    return "\n".join(lines)


def build_fva_context(fva_data: dict) -> str:
    return (
        f"Seat {fva_data.get('seat_number')} ({fva_data.get('seat_position_label')})\n"
        f"Action: {fva_data.get('action_type')} {fva_data.get('bet_amount')} BB"
    )


def build_hole_card_context(hand_start_state: dict) -> str:
    players = hand_start_state["hand_setup"]["players"]
    fva_seat = hand_start_state["fva"]["seat_number"]
    eligible = players if fva_seat is None else [p for p in players if p["seat_number"] <= fva_seat]

    lines = []
    for p in eligible:
        suffix = " — FVA, stop here" if p.get("seat_number") == fva_seat else ""
        lines.append(f"- Seat {p['seat_number']} ({p['seat_position_label']}){suffix}")
    return "\n".join(lines)
