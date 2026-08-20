# CLI entrypoint. Exposes `tt` command via the [project.scripts] entry
# in pyproject.toml. Subcommands: `tt ingest`, `tt materialize-clips`,
# `tt process-clips`, `tt process-hand-setups`, `tt process-hand-starts`.

import argparse
import asyncio
import sys
from pathlib import Path

from google.cloud import bigquery, storage

from .clip_materialization import MaterializeError, materialize_clips, materialize_clips_for_pending_videos
from .hand_action_processing import process_pending_hand_starts
from .hand_setup_processing import process_pending_clips
from .hand_start_processing import process_pending_hand_setups
from .ingest import process_manifest
from .reference_images import (
    STREET_REFERENCE_ORDER,
    load_reference_images,
    reference_image_filename,
)


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
    pc_parser.add_argument("--max-attempts", type=int, default=3)
    pc_parser.add_argument("--video-id")
    pc_parser.add_argument("--clip-id")

    phs_parser = subparsers.add_parser("process-hand-setups")
    phs_parser.add_argument("--project", required=True)
    phs_parser.add_argument("--dataset", required=True)
    phs_parser.add_argument("--videos-bucket", required=True)
    phs_parser.add_argument("--hand-starts-bucket", required=True)
    phs_parser.add_argument("--video-id")
    phs_parser.add_argument("--hand-setup-id")
    phs_parser.add_argument("--max-concurrent", type=int, default=4)
    phs_parser.add_argument("--max-attempts", type=int, default=3)

    pha_parser = subparsers.add_parser("process-hand-starts")
    pha_parser.add_argument("--project", required=True)
    pha_parser.add_argument("--dataset", required=True)
    pha_parser.add_argument("--videos-bucket", required=True)
    pha_parser.add_argument("--hand-actions-bucket", required=True)
    pha_parser.add_argument("--video-id")
    pha_parser.add_argument("--hand-start-id")
    pha_parser.add_argument("--max-concurrent", type=int, default=4)
    pha_parser.add_argument("--max-attempts", type=int, default=3)

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
        only_clip_ids = [args.clip_id] if args.clip_id else None

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
                only_clip_ids=only_clip_ids,
                max_attempts=args.max_attempts,
            )
        )
        for key, value in stats.items():
            print(f"{key}: {value}")
    elif args.command == "process-hand-setups":
        prompts_dir = Path(__file__).resolve().parents[2] / "prompts"

        identify_hand_start_path = prompts_dir / "identify_hand_start.md"
        if not identify_hand_start_path.exists():
            print(
                "prompts/identify_hand_start.md not found — see README for setup",
                file=sys.stderr,
            )
            sys.exit(1)

        extract_hole_cards_path = prompts_dir / "extract_hole_cards.md"
        if not extract_hole_cards_path.exists():
            print(
                "prompts/extract_hole_cards.md not found — see README for setup",
                file=sys.stderr,
            )
            sys.exit(1)

        identify_hand_start_prompt = identify_hand_start_path.read_text()
        extract_hole_cards_prompt = extract_hole_cards_path.read_text()

        stats = asyncio.run(
            process_pending_hand_setups(
                project_id=args.project,
                dataset=args.dataset,
                videos_bucket=args.videos_bucket,
                hand_starts_bucket=args.hand_starts_bucket,
                identify_hand_start_prompt=identify_hand_start_prompt,
                extract_hole_cards_prompt=extract_hole_cards_prompt,
                video_id=args.video_id,
                only_hand_setup_ids=(
                    [args.hand_setup_id] if args.hand_setup_id else None
                ),
                max_concurrent=args.max_concurrent,
                max_attempts=args.max_attempts,
            )
        )
        for key, value in stats.items():
            print(f"{key}: {value}")
    elif args.command == "process-hand-starts":
        prompts_dir = Path(__file__).resolve().parents[2] / "prompts"
        references_dir = Path(__file__).resolve().parents[2] / "references"

        prompt_paths = {}
        for name in (
            "extract_player_actions",
            "extract_community_cards",
            "extract_community_cards_from_frame",
        ):
            path = prompts_dir / f"{name}.md"
            if not path.exists():
                print(f"prompts/{name}.md not found — see README for setup", file=sys.stderr)
                sys.exit(1)
            prompt_paths[name] = path

        for street in STREET_REFERENCE_ORDER:
            filename = reference_image_filename(street)
            if not (references_dir / filename).exists():
                print(f"references/{filename} not found — see README for setup", file=sys.stderr)
                sys.exit(1)

        # Read once at startup, not per hand: a corpus run makes up to three
        # scan calls per hand and each carries all three images.
        reference_images = load_reference_images(references_dir)

        stats = asyncio.run(
            process_pending_hand_starts(
                project_id=args.project,
                dataset=args.dataset,
                videos_bucket=args.videos_bucket,
                hand_actions_bucket=args.hand_actions_bucket,
                extract_player_actions_prompt=prompt_paths["extract_player_actions"].read_text(),
                extract_community_cards_prompt=prompt_paths[
                    "extract_community_cards"
                ].read_text(),
                extract_community_cards_from_frame_prompt=prompt_paths[
                    "extract_community_cards_from_frame"
                ].read_text(),
                reference_images=reference_images,
                video_id=args.video_id,
                only_hand_start_ids=(
                    [args.hand_start_id] if args.hand_start_id else None
                ),
                max_concurrent=args.max_concurrent,
                max_attempts=args.max_attempts,
            )
        )
        for key, value in stats.items():
            print(f"{key}: {value}")
    else:
        parser.print_help()
        sys.exit(1)
