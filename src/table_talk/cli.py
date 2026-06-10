# CLI entrypoint. Exposes `tt` command via the [project.scripts] entry
# in pyproject.toml. Subcommands: `tt ingest`, `tt materialize-clips`,
# `tt process-clips`.

import argparse
import asyncio
import sys
from pathlib import Path

from google.cloud import bigquery, storage

from .clip_materialization import MaterializeError, materialize_clips, materialize_clips_for_pending_videos
from .hand_setup_processing import process_pending_clips
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

    pc_parser = subparsers.add_parser("process-clips")
    pc_parser.add_argument("--project", required=True)
    pc_parser.add_argument("--dataset", required=True)
    pc_parser.add_argument("--videos-bucket", required=True)
    pc_parser.add_argument("--hand-setups-bucket", required=True)
    pc_parser.add_argument("--max-concurrent", type=int, default=4)
    pc_parser.add_argument("--video-id")

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
    elif args.command == "process-clips":
        prompts_dir = Path(__file__).resolve().parents[2] / "prompts"

        identify_hand_path = prompts_dir / "identify_hand.md"
        if not identify_hand_path.exists():
            print(
                "prompts/identify_hand.md not found — see README for setup",
                file=sys.stderr,
            )
            sys.exit(1)

        extract_player_info_path = prompts_dir / "extract_player_info.md"
        if not extract_player_info_path.exists():
            print(
                "prompts/extract_player_info.md not found — see README for setup",
                file=sys.stderr,
            )
            sys.exit(1)

        identify_hand_prompt = identify_hand_path.read_text()
        extract_player_info_prompt = extract_player_info_path.read_text()
        only_video_ids = [args.video_id] if args.video_id else None

        stats = asyncio.run(
            process_pending_clips(
                project_id=args.project,
                dataset=args.dataset,
                videos_bucket=args.videos_bucket,
                hand_setups_bucket=args.hand_setups_bucket,
                identify_hand_prompt=identify_hand_prompt,
                extract_player_info_prompt=extract_player_info_prompt,
                max_concurrent=args.max_concurrent,
                only_video_ids=only_video_ids,
            )
        )
        for key, value in stats.items():
            print(f"{key}: {value}")
    else:
        parser.print_help()
        sys.exit(1)
