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
