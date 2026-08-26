# Schema source of truth: schemas/tournament_results.json
# TournamentResultsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# write_tournament_results has REPLACE semantics, keyed on video_id: every call
# deletes any existing rows for that video_id, then inserts the given rows, in
# one atomic multi-statement transaction. This makes the writer idempotent under
# retries — a reprocess overwrites rather than appends, which is what keeps
# deliberate reprocessing available after a prompt fix. The DELETE runs
# unconditionally, on every write, so the path that prevents duplicates is the
# same path exercised normally rather than a rare branch that only fires during
# an outage. It runs at write time after all processing, not as a pre-flight
# step, so a failure mid-processing cannot leave the video with zero rows. And
# it is atomic, so a committed DELETE followed by a failed INSERT cannot destroy
# a good row.
#
# The natural key is an explicit parameter, never derived from the rows, so an
# empty row list still issues its DELETE rather than being a no-op.
#
# This write is mutating DML, which BigQuery serializes per table rather than
# running with the concurrency available to INSERT-only writes. That is a
# throughput constraint, not a correctness one: an aborted transaction surfaces
# as a write error, is classified retryable, and the retry is safe precisely
# because the write is idempotent.
#
# Every scalar column is STRING or INT64. Payout and bounty amounts are floats
# and live inside tournament_results_state, which BigQuery parses server-side
# and which never reaches bq_param_type — that helper has no FLOAT64 branch and
# no None branch, and this writer does not filter None out of the row dict.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.tournament_results_row import TournamentResultsRow
from .bq_utils import bq_param_type, build_replace_sql


class TournamentResultsWriteError(Exception):
    pass


def write_tournament_results(
    rows: list[TournamentResultsRow],
    *,
    video_id: str,
    project_id: str,
    dataset: str,
    table: str = "tournament_results",
    client: bigquery.Client | None = None,
) -> None:
    for row in rows:
        if row.video_id != video_id:
            raise TournamentResultsWriteError(
                f"row.video_id={row.video_id!r} != video_id={video_id!r}"
            )
    if client is None:
        client = bigquery.Client(project=project_id)

    column_list = ""
    value_tuples = []
    query_parameters = [bigquery.ScalarQueryParameter("replace_key", "STRING", video_id)]
    if rows:
        first_dict = asdict(rows[0])
        columns = list(first_dict.keys())
        column_list = ", ".join(columns)

        for i, row in enumerate(rows):
            row_dict = asdict(row)
            placeholders = ", ".join(f"@{c}_{i}" for c in columns)
            value_tuples.append(f"({placeholders})")
            for c, v in row_dict.items():
                query_parameters.append(
                    bigquery.ScalarQueryParameter(f"{c}_{i}", bq_param_type(v), v)
                )

    fq_table = f"`{project_id}.{dataset}.{table}`"
    query = build_replace_sql(fq_table, "video_id", column_list, value_tuples)
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise TournamentResultsWriteError(str(exc)) from exc
    if job.errors:
        raise TournamentResultsWriteError(f"BQ DML errors: {job.errors}")
