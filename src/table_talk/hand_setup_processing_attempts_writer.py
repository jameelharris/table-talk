# Schema source of truth: schemas/hand_setup_processing_attempts.json
# HandSetupProcessingAttemptsRow is generated from that schema by
# scripts/gen_schemas.py. Drift is caught by codegen consistency
# and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The attempted_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_setup_processing_attempts_row import HandSetupProcessingAttemptsRow
from .bq_utils import bq_param_type

VALID_STATUSES: frozenset[str] = frozenset({
    "complete",
    "complete_skipped",
    "complete_uncontested",
    "failed_transient",
    "failed_permanent",
    "failed_parked",
})


class HandSetupProcessingAttemptsWriteError(Exception):
    pass


def write_hand_setup_processing_attempt_row(
    row: HandSetupProcessingAttemptsRow,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if row.status not in VALID_STATUSES:
        raise HandSetupProcessingAttemptsWriteError(
            f"Invalid status {row.status!r}. Must be one of: {sorted(VALID_STATUSES)}"
        )
    if client is None:
        client = bigquery.Client(project=project)
    row_dict = {k: v for k, v in asdict(row).items() if v is not None}
    columns = list(row_dict.keys())
    column_list = ", ".join(columns)
    placeholders = ", ".join(f"@{c}" for c in columns)
    table = f"{project}.{dataset}.hand_setup_processing_attempts"
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
        raise HandSetupProcessingAttemptsWriteError(str(exc)) from exc
    if job.errors:
        raise HandSetupProcessingAttemptsWriteError(f"BQ DML errors: {job.errors}")


def write_hand_setup_processing_attempt_rows(
    rows: list[HandSetupProcessingAttemptsRow],
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    """Append many attempt rows in one INSERT.

    The batched sibling of write_hand_setup_processing_attempt_row, for `tt mark-pending`,
    where one operator action marks every entity of a video pending. Per
    CLAUDE.md's "Batch DML for high-cardinality writes," the atomicity unit is
    the logical group that belongs together — here, one command's marks against
    one table.

    A bare INSERT, not a replace: state tables are append-only, so there is
    nothing to delete and no transaction to wrap. An empty list is therefore a
    no-op, unlike a stage writer, whose DELETE must run even with no rows.
    """
    if not rows:
        return
    for row in rows:
        if row.status not in VALID_STATUSES:
            raise HandSetupProcessingAttemptsWriteError(
                f"Invalid status {row.status!r}. Must be one of: {sorted(VALID_STATUSES)}"
            )
    if client is None:
        client = bigquery.Client(project=project)
    row_dicts = [{k: v for k, v in asdict(row).items() if v is not None} for row in rows]
    columns = list(row_dicts[0].keys())
    # One INSERT carries one column list, so every row must contribute the same
    # columns after None-filtering. Callers build these rows uniformly; a
    # disagreement is a caller bug, not an input case to accommodate.
    for row_dict in row_dicts[1:]:
        if list(row_dict.keys()) != columns:
            raise HandSetupProcessingAttemptsWriteError(
                f"Rows disagree on columns: {columns} vs {list(row_dict.keys())}"
            )
    column_list = ", ".join(columns)
    value_tuples = []
    query_parameters = []
    for i, row_dict in enumerate(row_dicts):
        placeholders = ", ".join(f"@{c}_{i}" for c in columns)
        value_tuples.append(f"({placeholders})")
        for c, v in row_dict.items():
            query_parameters.append(
                bigquery.ScalarQueryParameter(f"{c}_{i}", bq_param_type(v), v)
            )
    table = f"{project}.{dataset}.hand_setup_processing_attempts"
    query = f"INSERT INTO `{table}` ({column_list}) VALUES {', '.join(value_tuples)}"
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandSetupProcessingAttemptsWriteError(str(exc)) from exc
    if job.errors:
        raise HandSetupProcessingAttemptsWriteError(f"BQ DML errors: {job.errors}")
