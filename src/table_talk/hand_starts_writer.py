# Schema source of truth: schemas/hand_starts.json
# HandStartsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# write_hand_starts has REPLACE semantics, keyed on hand_setup_id: every
# call deletes any existing rows for that hand_setup_id, then inserts the
# given rows, in one atomic multi-statement transaction. This makes the
# writer idempotent under retries — a reprocess overwrites rather than
# appends. The DELETE runs unconditionally, including when rows is empty
# (a hand_setup can legitimately conclude with zero hand_starts rows).
# This write is now mutating DML, which BigQuery serializes per table
# (~2 concurrent, rest queued) rather than the fine-grained-DML path
# available to plain INSERT-only writes.
#
# verify_frame_gcs_paths is BigQuery REPEATED — it goes through
# ArrayQueryParameter, not bq_param_type/ScalarQueryParameter (which only
# handles scalar types). Callers must always supply a list (never None):
# SQL NULL and an empty array are not equivalent for a repeated column.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_starts_row import HandStartsRow
from .bq_utils import bq_param_type, build_replace_sql


class HandStartsWriteError(Exception):
    pass


def write_hand_starts(
    rows: list[HandStartsRow],
    *,
    hand_setup_id: str,
    project_id: str,
    dataset: str,
    table: str = "hand_starts",
    client: bigquery.Client | None = None,
) -> None:
    for row in rows:
        if row.hand_setup_id != hand_setup_id:
            raise HandStartsWriteError(
                f"row.hand_setup_id={row.hand_setup_id!r} != hand_setup_id={hand_setup_id!r}"
            )
    if client is None:
        client = bigquery.Client(project=project_id)

    column_list = ""
    value_tuples = []
    query_parameters = [bigquery.ScalarQueryParameter("replace_key", "STRING", hand_setup_id)]
    if rows:
        first_dict = asdict(rows[0])
        columns = list(first_dict.keys())
        column_list = ", ".join(columns)

        for i, row in enumerate(rows):
            row_dict = asdict(row)
            placeholders = ", ".join(f"@{c}_{i}" for c in columns)
            value_tuples.append(f"({placeholders})")
            for c, v in row_dict.items():
                param_name = f"{c}_{i}"
                if c == "verify_frame_gcs_paths":
                    query_parameters.append(bigquery.ArrayQueryParameter(param_name, "STRING", v))
                else:
                    query_parameters.append(bigquery.ScalarQueryParameter(param_name, bq_param_type(v), v))

    fq_table = f"`{project_id}.{dataset}.{table}`"
    query = build_replace_sql(fq_table, "hand_setup_id", column_list, value_tuples)
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandStartsWriteError(str(exc)) from exc
    if job.errors:
        raise HandStartsWriteError(f"BQ DML errors: {job.errors}")
