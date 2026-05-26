# Schema source of truth: schemas/clip_manifest.json
# ClipManifestRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The materialized_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.clip_manifest_row import ClipManifestRow


class ClipManifestWriteError(Exception):
    pass


def _bq_param_type(value: object) -> str:
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, int):
        return "INT64"
    raise ClipManifestWriteError(f"Unsupported parameter type: {type(value).__name__}")


def write_clip_manifest_rows(
    rows: list[ClipManifestRow],
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if not rows:
        return
    if client is None:
        client = bigquery.Client(project=project)

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
            query_parameters.append(bigquery.ScalarQueryParameter(f"{c}_{i}", _bq_param_type(v), v))

    query = f"INSERT INTO `{project}.{dataset}.clip_manifest` ({column_list}) VALUES {', '.join(value_tuples)}"
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise ClipManifestWriteError(str(exc)) from exc
    if job.errors:
        raise ClipManifestWriteError(f"BQ DML errors: {job.errors}")
