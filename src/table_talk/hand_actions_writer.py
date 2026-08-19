# Schema source of truth: schemas/hand_actions.json
# HandActionsRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The detected_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# write_hand_actions has REPLACE semantics, keyed on hand_start_id: every
# call deletes any existing rows for that hand_start_id, then inserts the
# given rows, in one atomic multi-statement transaction. This makes the
# writer idempotent under retries — a reprocess overwrites rather than
# appends. The DELETE runs unconditionally, including when rows is empty, so
# an empty row list is a real write rather than a no-op.
#
# Phase 5's orchestrator nonetheless never passes an empty list. Every
# `complete` outcome writes exactly one row (a hand that ends preflop still
# has a preflop street), and failures deliberately leave any existing row
# alone — per ARCHITECTURE.md, failures never delete existing output, so a
# stage row legitimately coexists with a later failed_transient or
# failed_parked attempt. The empty-list path is kept because it is the
# template's contract and Phase 4's uncontested branch relies on it.
#
# hand_start_id is both the natural key and the table's primary key.
# hand_actions is 1:1 with hand_starts, so there is no separate
# hand_action_id column. Keying on the entity the phase processes is
# CLAUDE.md's rule, and it matches hand_start_processing_attempts.
#
# street_frame_gcs_paths is BigQuery REPEATED — it goes through
# ArrayQueryParameter, not bq_param_type/ScalarQueryParameter (which only
# handles scalar types). Callers must always supply a list (never None):
# SQL NULL and an empty array are not equivalent for a repeated column.
#
# Every column in this table is REQUIRED, and that is load-bearing rather
# than incidental: bq_param_type has no None branch and no float branch,
# and this writer (unlike the state-table writers) does not filter None out
# of the row dict. A NULLABLE scalar or a FLOAT64 column cannot be added to
# hand_actions without extending bq_utils first. Per-row float values such
# as bet_amount live inside the hand_action_state JSON blob, which BQ
# parses server-side, so they never reach bq_param_type.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.hand_actions_row import HandActionsRow
from .bq_utils import bq_param_type, build_replace_sql

REPEATED_COLUMNS = frozenset({"street_frame_gcs_paths"})


class HandActionsWriteError(Exception):
    pass


def write_hand_actions(
    rows: list[HandActionsRow],
    *,
    hand_start_id: str,
    project_id: str,
    dataset: str,
    table: str = "hand_actions",
    client: bigquery.Client | None = None,
) -> None:
    for row in rows:
        if row.hand_start_id != hand_start_id:
            raise HandActionsWriteError(
                f"row.hand_start_id={row.hand_start_id!r} != hand_start_id={hand_start_id!r}"
            )
    if client is None:
        client = bigquery.Client(project=project_id)

    column_list = ""
    value_tuples = []
    query_parameters = [bigquery.ScalarQueryParameter("replace_key", "STRING", hand_start_id)]
    if rows:
        first_dict = asdict(rows[0])
        columns = list(first_dict.keys())
        column_list = ", ".join(columns)

        for i, row in enumerate(rows):
            row_dict = asdict(row)
            placeholders = ", ".join(f"@{c}_{i}" for c in columns)
            value_tuples.append(f"({placeholders})")
            for c, v in row_dict.items():
                param_name = f"{c}_{i}"
                if c in REPEATED_COLUMNS:
                    query_parameters.append(
                        bigquery.ArrayQueryParameter(param_name, "STRING", v)
                    )
                else:
                    query_parameters.append(
                        bigquery.ScalarQueryParameter(param_name, bq_param_type(v), v)
                    )

    fq_table = f"`{project_id}.{dataset}.{table}`"
    query = build_replace_sql(fq_table, "hand_start_id", column_list, value_tuples)
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise HandActionsWriteError(str(exc)) from exc
    if job.errors:
        raise HandActionsWriteError(f"BQ DML errors: {job.errors}")
