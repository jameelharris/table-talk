# CLI entrypoint. Exposes `tt` command via the [project.scripts] entry
# in pyproject.toml. Currently has one subcommand: `tt ingest`.

import argparse
import sys
from pathlib import Path

from google.cloud import bigquery, storage

from .ingest import process_manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="tt")
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--manifest", required=True, type=Path)
    ingest_parser.add_argument("--project", required=True)
    ingest_parser.add_argument("--dataset", required=True)
    ingest_parser.add_argument("--bucket", required=True)

    args = parser.parse_args()

    if args.command == "ingest":
        bq_client = bigquery.Client(project=args.project)
        storage_client = storage.Client()
        process_manifest(
            args.manifest,
            project=args.project,
            dataset=args.dataset,
            bucket=args.bucket,
            bq_client=bq_client,
            storage_client=storage_client,
        )
    else:
        parser.print_help()
        sys.exit(1)
