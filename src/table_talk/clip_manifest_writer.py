# Schema source of truth: schemas/clip_manifest.json
# ClipManifestRow is generated from that schema by scripts/gen_schemas.py.
# Drift is caught by codegen consistency and at integration-test time.
# Uses DML INSERT (not load_table_from_json) so BQ applies column
# DEFAULTs server-side. The materialized_at column is populated
# by BQ via CURRENT_TIMESTAMP() per the schema's defaultValueExpression.
#
# write_clip_manifest_rows has REPLACE semantics, keyed on video_id: every
# call deletes any existing rows for that video_id, then inserts the given
# rows, in one atomic multi-statement transaction. This makes the writer
# idempotent under retries — a re-materialization overwrites rather than
# appends. It became load-bearing when Phase 2 moved to a state table: a
# video whose latest attempt is 'blocked_upstream' or 'failed_transient' is
# re-selected even when it already has clip_manifest rows, a state that could
# not arise while pending-ness was derived from the absence of those rows.
# Without the replace wrapper that write would be an INSERT on top of the
# existing rows — duplicate clip windows for one video, every one of them
# feeding Phase 3. The DELETE runs unconditionally, including when rows is
# empty. This write is now mutating DML, which BigQuery serializes per table
# rather than the fine-grained-DML path available to plain INSERT-only writes;
# Phase 2 processes videos sequentially with one statement each, so that is
# not a practical constraint here.

from dataclasses import asdict

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from ._generated.clip_manifest_row import ClipManifestRow
from .bq_utils import bq_param_type, build_replace_sql


class ClipManifestWriteError(Exception):
    pass


def write_clip_manifest_rows(
    rows: list[ClipManifestRow],
    *,
    video_id: str,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    for row in rows:
        if row.video_id != video_id:
            raise ClipManifestWriteError(
                f"row.video_id={row.video_id!r} != video_id={video_id!r}"
            )
    if client is None:
        client = bigquery.Client(project=project)

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

    fq_table = f"`{project}.{dataset}.clip_manifest`"
    query = build_replace_sql(fq_table, "video_id", column_list, value_tuples)
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    try:
        job = client.query(query, job_config=job_config)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise ClipManifestWriteError(str(exc)) from exc
    if job.errors:
        raise ClipManifestWriteError(f"BQ DML errors: {job.errors}")
