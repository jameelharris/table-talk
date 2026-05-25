# Schema source of truth: schemas/videos.json
# VideosRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The ingested_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.videos_row import VideosRow


class VideosWriteError(Exception):
    pass


def _bq_param_type(value: object) -> str:
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, int):
        return "INT64"
    raise VideosWriteError(f"Unsupported parameter type: {type(value).__name__}")


def write_video_row(
    row: VideosRow,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if client is None:
        client = bigquery.Client(project=project)
    row_dict = asdict(row)
    columns = list(row_dict.keys())
    column_list = ", ".join(columns)
    placeholders = ", ".join(f"@{c}" for c in columns)
    query = f"INSERT INTO `{project}.{dataset}.videos` ({column_list}) VALUES ({placeholders})"
    query_parameters = [
        bigquery.ScalarQueryParameter(c, _bq_param_type(v), v)
        for c, v in row_dict.items()
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise VideosWriteError(str(exc)) from exc
    if job.errors:
        raise VideosWriteError(f"BQ DML errors: {job.errors}")
