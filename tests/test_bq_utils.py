import pytest

from table_talk.bq_utils import bq_param_type


def test_str_maps_to_string():
    assert bq_param_type("hello") == "STRING"


def test_int_maps_to_int64():
    assert bq_param_type(42) == "INT64"


def test_dict_maps_to_json():
    assert bq_param_type({"key": "value"}) == "JSON"


def test_bool_maps_to_int64_subclass_of_int():
    # bool is a subclass of int; documents current behavior.
    # If a BOOL column is ever added, this needs revisiting.
    assert bq_param_type(True) == "INT64"


@pytest.mark.parametrize("value", [1.5, None, [1, 2, 3], object()])
def test_unsupported_type_raises_type_error(value):
    with pytest.raises(TypeError, match="Unsupported parameter type"):
        bq_param_type(value)
