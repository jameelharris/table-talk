from table_talk.card_normalization import normalize_card, normalize_cards


def test_normalize_card_ten():
    assert normalize_card("10s") == "Ts"


def test_normalize_card_non_ten_unchanged():
    assert normalize_card("Ks") == "Ks"


def test_normalize_card_none():
    assert normalize_card(None) is None


def test_normalize_cards_mixed_list():
    assert normalize_cards(["10s", "Ks", None, "10d"]) == ["Ts", "Ks", None, "Td"]
