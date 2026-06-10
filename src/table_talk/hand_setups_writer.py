# Schema source of truth: schemas/hand_setups.json
# HandSetupsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.

import json
from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_setups_row import HandSetupsRow


class HandSetupsWriteError(Exception):
    pass


def _bq_param_type(value: object) -> str:
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, dict):
        return "JSON"
    raise HandSetupsWriteError(f"Unsupported parameter type: {type(value).__name__}")


def write_hand_setups(
    rows: list[HandSetupsRow],
    *,
    project_id: str,
    dataset: str,
    table: str = "hand_setups",
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
            bq_value = json.dumps(v) if isinstance(v, dict) else v
            query_parameters.append(
                bigquery.ScalarQueryParameter(f"{c}_{i}", _bq_param_type(v), bq_value)
            )

    query = (
        f"INSERT INTO `{project_id}.{dataset}.{table}` ({column_list}) "
        f"VALUES {', '.join(value_tuples)}"
    )
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandSetupsWriteError(str(exc)) from exc
    if job.errors:
        raise HandSetupsWriteError(f"BQ DML errors: {job.errors}")
