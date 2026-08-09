# Schema source of truth: schemas/hand_setups.json
# HandSetupsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# write_hand_setups has REPLACE semantics, keyed on clip_id: every call
# deletes any existing rows for that clip_id, then inserts the given rows,
# in one atomic multi-statement transaction. This makes the writer
# idempotent under retries — a reprocess overwrites rather than appends,
# and a batch that shrinks on reprocess correctly drops the now-stale rows
# rather than orphaning them. The DELETE runs unconditionally, including
# when rows is empty (a clip can legitimately conclude with zero
# hand_setups rows). This write is now mutating DML, which BigQuery
# serializes per table (~2 concurrent, rest queued) rather than the
# fine-grained-DML path available to plain INSERT-only writes.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_setups_row import HandSetupsRow
from .bq_utils import bq_param_type, build_replace_sql


class HandSetupsWriteError(Exception):
    pass


def write_hand_setups(
    rows: list[HandSetupsRow],
    *,
    clip_id: str,
    project_id: str,
    dataset: str,
    table: str = "hand_setups",
    client: bigquery.Client | None = None,
) -> None:
    for row in rows:
        if row.clip_id != clip_id:
            raise HandSetupsWriteError(f"row.clip_id={row.clip_id!r} != clip_id={clip_id!r}")
    if client is None:
        client = bigquery.Client(project=project_id)

    column_list = ""
    value_tuples = []
    query_parameters = [bigquery.ScalarQueryParameter("replace_key", "STRING", clip_id)]
    if rows:
        first_dict = asdict(rows[0])
        columns = list(first_dict.keys())
        column_list = ", ".join(columns)

        for i, row in enumerate(rows):
            row_dict = asdict(row)
            placeholders = ", ".join(f"@{c}_{i}" for c in columns)
            value_tuples.append(f"({placeholders})")
            for c, v in row_dict.items():
                bq_value = v
                query_parameters.append(
                    bigquery.ScalarQueryParameter(f"{c}_{i}", bq_param_type(v), bq_value)
                )

    fq_table = f"`{project_id}.{dataset}.{table}`"
    query = build_replace_sql(fq_table, "clip_id", column_list, value_tuples)
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandSetupsWriteError(str(exc)) from exc
    if job.errors:
        raise HandSetupsWriteError(f"BQ DML errors: {job.errors}")
