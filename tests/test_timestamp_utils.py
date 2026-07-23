import pytest

from table_talk.timestamp_utils import parse_timestamp


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
