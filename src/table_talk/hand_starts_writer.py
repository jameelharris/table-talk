# Schema source of truth: schemas/hand_starts.json
# HandStartsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# verify_frame_gcs_paths is BigQuery REPEATED — it goes through
# ArrayQueryParameter, not bq_param_type/ScalarQueryParameter (which only
# handles scalar types). Callers must always supply a list (never None):
# SQL NULL and an empty array are not equivalent for a repeated column.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_starts_row import HandStartsRow
from .bq_utils import bq_param_type


class HandStartsWriteError(Exception):
    pass


def write_hand_starts(
    rows: list[HandStartsRow],
    *,
    project_id: str,
    dataset: str,
    table: str = "hand_starts",
    client: bigquery.Client | None = None,
) -> None:
    if not rows:
        return
    if client is None:
        client = bigquery.Client(project=project_id)

    first_dict = asdict(rows[0])
    columns = list(first_dict.keys())
    column_list = ", ".join(columns)

    value_tuples = []
    query_parameters = []
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

    query = (
        f"INSERT INTO `{project_id}.{dataset}.{table}` ({column_list}) "
        f"VALUES {', '.join(value_tuples)}"
    )
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandStartsWriteError(str(exc)) from exc
    if job.errors:
        raise HandStartsWriteError(f"BQ DML errors: {job.errors}")
