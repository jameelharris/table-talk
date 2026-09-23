import pytest

from table_talk.timestamp_utils import format_timestamp, parse_timestamp


@pytest.mark.parametrize("s,expected", [
    ("05:32", 332),
    ("00:00", 0),
    ("59:59", 3599),
    ("01:00:00", 3600),
    ("02:30:45", 9045),
    ("23:59:59", 86399),
])
def test_parse_timestamp_valid(s, expected):
    assert parse_timestamp(s) == expected


@pytest.mark.parametrize("s", [
    "",
    "5:32:10:00",
    "05",
    "5.5",
    "5:abc",
    "-5:32",
])
def test_parse_timestamp_invalid(s):
    with pytest.raises(ValueError):
        parse_timestamp(s)


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00"),
    (105, "01:45"),
    (332, "05:32"),
    (2359, "39:19"),
    (3599, "59:59"),
    (3600, "01:00:00"),
    (9045, "02:30:45"),
    (86399, "23:59:59"),
])
def test_format_timestamp_valid(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_format_timestamp_negative_raises():
    with pytest.raises(ValueError):
        format_timestamp(-1)


@pytest.mark.parametrize("seconds", [0, 105, 3599, 3600, 9045, 86399])
def test_format_timestamp_round_trips_through_parse(seconds):
    assert parse_timestamp(format_timestamp(seconds)) == seconds
