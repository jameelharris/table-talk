def normalize_card(card: str | None) -> str | None:
    if card is None:
        return None
    return ("T" + card[2:]) if card.startswith("10") else card


def normalize_cards(cards: list[str | None]) -> list[str | None]:
    return [normalize_card(c) for c in cards]
