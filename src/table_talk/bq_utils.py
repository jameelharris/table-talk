def bq_param_type(value: object) -> str:
    # NOTE: bool is a subclass of int — if a BOOL column is ever added,
    # check isinstance(value, bool) BEFORE int, or True/False maps to INT64.
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, dict):
        return "JSON"
    raise TypeError(f"Unsupported parameter type: {type(value).__name__}")


def build_replace_sql(
    fq_table: str,
    key_column: str,
    column_list: str,
    value_tuples: list[str],
    key_param: str = "replace_key",
) -> str:
    """Build a DELETE-then-INSERT statement that makes fq_table hold exactly
    the given rows for the entity identified by @{key_param}.

    fq_table must already be backtick-quoted, e.g. f"`{project}.{dataset}.{table}`".
    The DELETE always runs. With no value_tuples, returns a bare DELETE
    (already atomic on its own). Otherwise wraps DELETE + INSERT in a
    multi-statement transaction so a failed INSERT rolls the DELETE back.
    """
    delete_sql = f"DELETE FROM {fq_table} WHERE {key_column} = @{key_param}"
    if not value_tuples:
        return delete_sql
    insert_sql = f"INSERT INTO {fq_table} ({column_list}) VALUES {', '.join(value_tuples)}"
    return f"BEGIN TRANSACTION; {delete_sql}; {insert_sql}; COMMIT TRANSACTION;"
