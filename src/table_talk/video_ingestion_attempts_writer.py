# Schema source of truth: terraform/environments/dev/schemas/video_ingestion_attempts.json
# The AttemptRow dataclass below must be kept in sync with that file.
# Drift is caught at integration-test time.

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

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
    table_ref = f"{project}.{dataset}.video_ingestion_attempts"
    row_dict = {**asdict(row), "attempted_at": datetime.now(UTC).isoformat()}
    try:
        job = client.load_table_from_json([row_dict], table_ref)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise AttemptsWriteError(str(exc)) from exc
    if job.errors:
        raise AttemptsWriteError(f"BQ load job errors: {job.errors}")
