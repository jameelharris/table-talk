# CLI entrypoint. Exposes `tt` command via the [project.scripts] entry
# in pyproject.toml. Subcommands: `tt ingest`, `tt materialize-clips`.

import argparse
import sys
from pathlib import Path

from google.cloud import bigquery, storage

from .clip_materialization import MaterializeError, materialize_clips, materialize_clips_for_pending_videos
from .ingest import process_manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="tt")
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--manifest", required=True, type=Path)
    ingest_parser.add_argument("--project", required=True)
    ingest_parser.add_argument("--dataset", required=True)
    ingest_parser.add_argument("--bucket", required=True)

    mat_parser = subparsers.add_parser("materialize-clips")
    mat_parser.add_argument("--project", required=True)
    mat_parser.add_argument("--dataset", required=True)
    mat_parser.add_argument("--video-id")

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
    elif args.command == "materialize-clips":
        bq_client = bigquery.Client(project=args.project)
        if args.video_id:
            try:
                materialize_clips(
                    args.video_id,
                    project=args.project,
                    dataset=args.dataset,
                    bq_client=bq_client,
                )
            except MaterializeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
        else:
            materialize_clips_for_pending_videos(
                project=args.project,
                dataset=args.dataset,
                bq_client=bq_client,
            )
    else:
        parser.print_help()
        sys.exit(1)
