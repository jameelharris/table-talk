#!/usr/bin/env python
"""Count the input tokens of Phase 5's clip-mode calls against real stored windows.

The motivating case is YzKyFMQ1avU t=3986, whose step D died on
`400 INVALID_ARGUMENT ... the input token count is 65577 but model only supports
up to 65536` on a 201 s window, while a 226 s window on the same video completed
36 minutes earlier in the same run. Nothing in the pipeline counts tokens, and
the two observations cannot both fit a cost model that is linear in window
length -- so the open question is whether our count is stable and the limit
varies, or the count itself varies.

CountTokens answers it for free: "There is no charge or quota restriction for
using the CountTokens API" (Vertex docs), capped at 3,000 rpm. This script makes
no generate_content call and bills nothing.

WHY HANDS ARE KEYED BY (video_id, hand_setup_time_seconds)
----------------------------------------------------------
Ids are positional -- hand_setup_id is {clip_id}_{NNN} and renumbers whenever a
clip's detection count changes -- so an id recorded in one run may point at a
different moment in the next, or not exist. The same rule repro_card_read.py
follows for frames. The timestamp is the stable anchor.

WHAT MUST STAY IN SYNC
----------------------
call_gemini_for_clip builds its parts inline and offers no way to obtain them
without issuing the call, so the Part construction below is duplicated from
gemini_caller.py:251-277. A drifted shape measures a request we never send.
Everything else -- the prompts, the context builders, the reference-image
loader, the model constant, the "global" location -- is imported from
production.

Usage:
    uv run python scripts/count_clip_tokens.py \
        --project table-talk-497020 \
        --dataset table_talk_dev \
        --videos-bucket table-talk-497020-videos-dev
"""

import argparse
import sys
from pathlib import Path

from google import genai
from google.cloud import bigquery
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from table_talk.gemini_caller import HAND_ACTION_CLIP_MODEL  # noqa: E402
from table_talk.prompt_context import (  # noqa: E402
    build_action_context,
    build_fva_context,
)
from table_talk.reference_images import load_reference_images  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
REFERENCES_DIR = REPO_ROOT / "references"

# gemini_caller's own default, and the only location any production caller uses.
LOCATION = "global"

# The four longest windows in the corpus, (video_id, hand_setup_time_seconds).
# The 2026-10-01 run completed the first three and 400'd on the fourth.
TARGETS = (
    ("YzKyFMQ1avU", 948),
    ("MPBLfM4mwfE", 2038),
    ("MPBLfM4mwfE", 1285),
    ("YzKyFMQ1avU", 3986),
)

# The step-D and step-E-scan user turns, verbatim from hand_action_processing.
STEP_D_USER_TEXT = "Extract the complete voluntary action sequence from this video clip."
SCAN_USER_TEXT = "Scan this clip and find when the {street_name} cards appear."

# The ceiling the 400 reported, for the headroom column.
REPORTED_LIMIT = 65536


def fetch_targets(client: bigquery.Client, project_id: str, dataset: str) -> list:
    """The four hands with the window the pending query would derive for them.

    raw_lead_gap_seconds is recomputed with the pending query's own expression
    (hand_action_processing.py:216-230) rather than read from a column -- it is
    not stored anywhere, and a hand-rolled LEAD would be a second definition of
    the window.
    """
    query = f"""
        WITH windowed AS (
          SELECT
            hs.hand_setup_id,
            hs.hand_setup_time_seconds,
            COALESCE(
              LEAD(hs.hand_setup_time_seconds) OVER (
                PARTITION BY hs.video_id
                ORDER BY hs.hand_setup_time_seconds, hs.hand_setup_id
              ),
              v.duration_seconds
            ) - hs.hand_setup_time_seconds AS raw_lead_gap_seconds
          FROM `{project_id}.{dataset}.hand_setups` hs
          INNER JOIN `{project_id}.{dataset}.videos` v USING (video_id)
        )
        SELECT
          h.hand_start_id,
          h.video_id,
          h.fva_time_seconds,
          h.hand_start_state,
          w.hand_setup_time_seconds,
          w.raw_lead_gap_seconds
        FROM `{project_id}.{dataset}.hand_starts` h
        INNER JOIN windowed w USING (hand_setup_id)
        WHERE CONCAT(h.video_id, ':', CAST(w.hand_setup_time_seconds AS STRING))
              IN UNNEST(@targets)
        ORDER BY w.raw_lead_gap_seconds DESC
    """
    # One STRING array of "video_id:seconds" keys rather than an array of
    # structs: ArrayQueryParameter's second argument is a scalar type name, and
    # an array of structs has to be built from StructQueryParameter entries --
    # more machinery than a fixed four-row lookup needs. The pair is still the
    # key, just spelled as one value on both sides.
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "targets",
                "STRING",
                [f"{video_id}:{seconds}" for video_id, seconds in TARGETS],
            )
        ]
    )
    return list(client.query(query, job_config=job_config).result())


def video_part(video_gcs_uri: str, start_offset_seconds: int, end_offset_seconds: int):
    """Duplicated from gemini_caller.call_gemini_for_clip -- keep in sync."""
    return types.Part(
        file_data=types.FileData(file_uri=video_gcs_uri, mime_type="video/*"),
        video_metadata=types.VideoMetadata(
            start_offset=f"{start_offset_seconds}s",
            end_offset=f"{end_offset_seconds}s",
            fps=1.0,
        ),
    )


def reference_parts(reference_images):
    """Duplicated from gemini_caller.call_gemini_for_clip -- keep in sync."""
    parts = []
    for image_bytes, mime_type, image_label in reference_images:
        parts.append(types.Part(text=f"Reference image — {image_label}:"))
        parts.append(types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime_type)))
    return parts


def count(client: genai.Client, system_instruction: str, parts: list) -> int:
    response = client.models.count_tokens(
        model=HAND_ACTION_CLIP_MODEL,
        contents=types.Content(role="user", parts=parts),
        config=types.CountTokensConfig(system_instruction=system_instruction),
    )
    return response.total_tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--videos-bucket", required=True)
    args = parser.parse_args()

    actions_prompt = (PROMPTS_DIR / "extract_player_actions.md").read_text()
    scan_prompt = (PROMPTS_DIR / "identify_community_cards.md").read_text()
    reference_images = load_reference_images(REFERENCES_DIR)

    bq = bigquery.Client(project=args.project)
    rows = fetch_targets(bq, args.project, args.dataset)
    if len(rows) != len(TARGETS):
        print(
            f"expected {len(TARGETS)} hands, got {len(rows)} — "
            "re-derive the timestamps against hand_setups",
            file=sys.stderr,
        )

    client = genai.Client(vertexai=True, project=args.project, location=LOCATION)
    print(f"model={HAND_ACTION_CLIP_MODEL} location={LOCATION}\n")

    for row in rows:
        video_gcs_uri = f"gs://{args.videos_bucket}/{row.video_id}.mp4"
        window_end = row.hand_setup_time_seconds + row.raw_lead_gap_seconds

        filled_actions_prompt = (
            actions_prompt
            .replace("{player_context}", build_action_context(row.hand_start_state))
            .replace(
                "{fva_context}",
                build_fva_context(row.hand_start_state["fva"], row.fva_time_seconds),
            )
        )
        d_text_only = count(
            client, filled_actions_prompt, [types.Part(text=STEP_D_USER_TEXT)]
        )
        d_total = count(
            client,
            filled_actions_prompt,
            [
                video_part(video_gcs_uri, row.hand_setup_time_seconds, window_end),
                types.Part(text=STEP_D_USER_TEXT),
            ],
        )

        filled_scan_prompt = scan_prompt.replace("{street_name}", "flop")
        scan_user_text = SCAN_USER_TEXT.format(street_name="flop")
        scan_fixed = count(
            client,
            filled_scan_prompt,
            [*reference_parts(reference_images), types.Part(text=scan_user_text)],
        )
        scan_no_images = count(
            client, filled_scan_prompt, [types.Part(text=scan_user_text)]
        )
        scan_window = window_end - row.fva_time_seconds
        scan_total = count(
            client,
            filled_scan_prompt,
            [
                video_part(video_gcs_uri, row.fva_time_seconds, window_end),
                *reference_parts(reference_images),
                types.Part(text=scan_user_text),
            ],
        )

        print(f"{row.hand_start_id}  (t={row.hand_setup_time_seconds})")
        print(
            f"  step D       window={row.raw_lead_gap_seconds:>4}s  "
            f"total={d_total:>6}  text_only={d_text_only:>5}  "
            f"video={d_total - d_text_only:>6}  "
            f"({(d_total - d_text_only) / row.raw_lead_gap_seconds:.1f} tok/s)  "
            f"headroom={REPORTED_LIMIT - d_total:>+7}"
        )
        print(
            f"  step E flop  window={scan_window:>4}s  "
            f"total={scan_total:>6}  fixed={scan_fixed:>5}  "
            f"video={scan_total - scan_fixed:>6}  "
            f"ref_images={scan_fixed - scan_no_images:>5}  "
            f"headroom={REPORTED_LIMIT - scan_total:>+7}"
        )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
