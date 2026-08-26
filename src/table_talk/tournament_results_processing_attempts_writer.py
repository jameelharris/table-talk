# Schema source of truth: schemas/tournament_results_processing_attempts.json
# TournamentResultsProcessingAttemptsRow is generated from that schema by
# scripts/gen_schemas.py. Drift is caught by codegen consistency
# and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The attempted_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# This is a state table: insert-only, one row per attempt, never updated and
# never deleted. To reverse a prior outcome, append a new row that supersedes
# it — an un-park is an appended retryable status, not an edit to the parked
# row.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.tournament_results_processing_attempts_row import (
    TournamentResultsProcessingAttemptsRow,
)
from .bq_utils import bq_param_type

VALID_STATUSES: frozenset[str] = frozenset({
    "complete",
    "complete_skipped",
    "failed_transient",
    "failed_permanent",
    "failed_parked",
})


class TournamentResultsProcessingAttemptsWriteError(Exception):
    pass


def write_tournament_results_processing_attempt_row(
    row: TournamentResultsProcessingAttemptsRow,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if row.status not in VALID_STATUSES:
        raise TournamentResultsProcessingAttemptsWriteError(
            f"Invalid status {row.status!r}. Must be one of: {sorted(VALID_STATUSES)}"
        )
    if client is None:
        client = bigquery.Client(project=project)
    row_dict = {k: v for k, v in asdict(row).items() if v is not None}
    columns = list(row_dict.keys())
    column_list = ", ".join(columns)
    placeholders = ", ".join(f"@{c}" for c in columns)
    table = f"{project}.{dataset}.tournament_results_processing_attempts"
    query = f"INSERT INTO `{table}` ({column_list}) VALUES ({placeholders})"
    query_parameters = [
        bigquery.ScalarQueryParameter(c, bq_param_type(v), v)
        for c, v in row_dict.items()
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise TournamentResultsProcessingAttemptsWriteError(str(exc)) from exc
    if job.errors:
        raise TournamentResultsProcessingAttemptsWriteError(f"BQ DML errors: {job.errors}")
