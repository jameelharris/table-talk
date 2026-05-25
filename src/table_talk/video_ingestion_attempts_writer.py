# Schema source of truth: schemas/video_ingestion_attempts.json
# The AttemptRow dataclass below must be kept in sync with that file.
# Drift is caught at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The attempted_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.

from dataclasses import asdict, dataclass

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

VALID_STATUSES: frozenset[str] = frozenset({
    "complete",
    "failed_terminal",
    "failed_transient_predownload",
    "failed_transient_postdownload",
})


@dataclass(frozen=True)
class AttemptRow:
    source_url: str
    status: str
    video_id: str | None = None
    status_message: str | None = None
    duration_ms: int | None = None


class AttemptsWriteError(Exception):
    pass


def _bq_param_type(value: object) -> str:
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, int):
        return "INT64"
    raise AttemptsWriteError(f"Unsupported parameter type: {type(value).__name__}")


def write_attempt_row(
    row: AttemptRow,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if row.status not in VALID_STATUSES:
        raise AttemptsWriteError(
            f"Invalid status {row.status!r}. Must be one of: {sorted(VALID_STATUSES)}"
        )
    if client is None:
        client = bigquery.Client(project=project)
    row_dict = {k: v for k, v in asdict(row).items() if v is not None}
    columns = list(row_dict.keys())
    column_list = ", ".join(columns)
    placeholders = ", ".join(f"@{c}" for c in columns)
    table = f"{project}.{dataset}.video_ingestion_attempts"
    query = f"INSERT INTO `{table}` ({column_list}) VALUES ({placeholders})"
    query_parameters = [
        bigquery.ScalarQueryParameter(c, _bq_param_type(v), v)
        for c, v in row_dict.items()
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise AttemptsWriteError(str(exc)) from exc
    if job.errors:
        raise AttemptsWriteError(f"BQ DML errors: {job.errors}")
