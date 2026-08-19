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


def build_action_context(hand_start_state: dict) -> str:
    # Step D's per-seat anchor set: position, stack, AND hole cards. Three
    # anchors make misattribution less likely than two — the same reasoning
    # behind Phase 4's stack-anchor fix for hole-card extraction.
    #
    # Every seat is emitted, not just the FVA-eligible ones (which is what
    # build_hole_card_context does): D tracks the hand through to showdown, so
    # a seat that acts postflop has to be in the context.
    lines = []
    for p in hand_start_state["hand_setup"]["players"]:
        cards = p.get("hole_cards")
        # Unknown cards are emitted as "unknown" rather than omitted, so the
        # seat survives as a position+stack anchor. Phase 4 can also leave a
        # half-read pair (e.g. ["Ah", None]); half a hand is not a usable
        # anchor, so it reads the same as no read at all.
        readable = cards and all(c is not None for c in cards)
        cards_str = " ".join(cards) if readable else "unknown"
        lines.append(
            f"- Seat {p['seat_number']} ({p['seat_position_label']}) | "
            f"Stack: {p.get('stack_size')} BB | Hole cards: {cards_str}"
        )
    return "\n".join(lines)


def build_prior_cards_context(prior_cards: list[str]) -> str:
    # extract_community_cards_from_frame.md keys the number of cards to read off
    # the number of prior cards (0 -> 3, 3 -> 1, 4 -> 1), so the count is stated
    # outright rather than left to be inferred from the list.
    if not prior_cards:
        return "(none — 0 prior cards)"
    listed = "\n".join(f"- {card}" for card in prior_cards)
    return f"{listed}\n\n({len(prior_cards)} prior cards)"


def build_hole_card_context(hand_start_state: dict) -> str:
    # The FVA's seat_number bounds the eligible set (seats 1..FVA inclusive),
    # but step C now reads every eligible seat uniformly by stack — no
    # per-seat FVA flag is emitted.
    players = hand_start_state["hand_setup"]["players"]
    fva_seat = hand_start_state["fva"]["seat_number"]
    eligible = players if fva_seat is None else [p for p in players if p["seat_number"] <= fva_seat]

    lines = [
        f"- Seat {p['seat_number']} ({p['seat_position_label']}) | Stack: {p.get('stack_size')} BB"
        for p in eligible
    ]
    return "\n".join(lines)
