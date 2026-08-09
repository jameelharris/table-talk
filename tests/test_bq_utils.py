import pytest

from table_talk.bq_utils import bq_param_type, build_replace_sql


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


def test_build_replace_sql_empty_value_tuples_is_bare_delete():
    sql = build_replace_sql("`proj.ds.t`", "clip_id", "", [])
    assert sql == "DELETE FROM `proj.ds.t` WHERE clip_id = @replace_key"
    assert "BEGIN TRANSACTION" not in sql
    assert "INSERT" not in sql


def test_build_replace_sql_non_empty_wraps_transaction_in_order():
    sql = build_replace_sql("`proj.ds.t`", "clip_id", "a, b", ["(@a_0, @b_0)"])
    begin_idx = sql.index("BEGIN TRANSACTION")
    delete_idx = sql.index("DELETE FROM `proj.ds.t` WHERE clip_id = @replace_key")
    insert_idx = sql.index("INSERT INTO `proj.ds.t` (a, b) VALUES (@a_0, @b_0)")
    commit_idx = sql.index("COMMIT TRANSACTION")
    assert begin_idx < delete_idx < insert_idx < commit_idx


def test_build_replace_sql_multiple_value_tuples_joined():
    sql = build_replace_sql("`proj.ds.t`", "clip_id", "a", ["(@a_0)", "(@a_1)"])
    assert "VALUES (@a_0), (@a_1)" in sql


def test_build_replace_sql_custom_key_param_honored():
    sql = build_replace_sql("`proj.ds.t`", "clip_id", "", [], key_param="my_key")
    assert sql == "DELETE FROM `proj.ds.t` WHERE clip_id = @my_key"
