#!/usr/bin/env python
"""Measure the gap between a clip call's billed prompt tokens and its CountTokens count.

THE OBSERVATION THIS EXISTS TO EXPLAIN
--------------------------------------
In the 2026-10-02 Phase 5 run, two step D calls billed far above their counted
size, and the excess did not scale with the window:

    YzKyFMQ1avU_004_001_001   91 s    counted  32,251    billed 157,741   +125,490
    YzKyFMQ1avU_017_003_001  201 s    counted  65,147    billed 185,983   +120,836

The two deltas agree within 3.7% while the windows differ by 2.2x, so the excess
behaves like a fixed addition of roughly 123,000 tokens rather than a change in
the per-second rate. Everything upstream of the boundary was ruled out
read-only: the request `call_gemini_for_clip` builds is unchanged since
2026-09-28, the SDK is pinned, and both rows record the same model, media
resolution and prompt blob hash the script counted against.

WHAT IT FOUND, AND WHY IT IS STILL HERE
---------------------------------------
The excess is audio, and the mechanism is that `video_metadata`'s start/end
offsets bound the frame sampling but not the audio track. Audio bills at 25
tokens/second in both cases; what varies is what those seconds are measured
over. Confirmed to the token on two videos of different lengths:

    YzKyFMQ1avU   5,147 s   5,508 text + 258*20 video + 25*5147 audio = 139,343
    MPBLfM4mwfE   3,015 s   5,419 text + 258*20 video + 25*3015 audio =  85,954

**It is intermittent.** A 6-hand Phase 5 run on YzKyFMQ1avU at concurrency 3
had three step D calls charged windowed audio (250, 350, 425 = 25 * window) and
three charged the whole file's 128,675, with nothing differing in the requests.
That matches what the 65,536-token input limit already does -- enforced
inconsistently across the backends the `global` endpoint fans out to, and
Google's own docs say a global request "may be processed in any Google Cloud
location around the world" with no way to know which.

So the open question is no longer what the charge is but whether any single
backend is consistent about it, which is what --location exists to test: a
region that windows the audio on every call would make pinning Phase 5's clip
calls to it the fix. Each run prints AUDIO_BILLING=windowed or
AUDIO_BILLING=whole_file, so a series of runs can be tallied with grep.

COST
----
One billed Pro call per run. A windowed call on a ~20 s window is about $0.01;
a whole-file one is about $0.17. CountTokens is free and unbilled. Nothing here
writes to BigQuery -- the two queried tables are read to render the prompt, and
no attempt row, stage row or GCS object is produced.

WHY IT CALLS PRODUCTION RATHER THAN REBUILDING THE REQUEST
----------------------------------------------------------
The billed call goes through `call_gemini_for_clip` itself, so there is no
second definition of the request shape to drift from the one production sends --
the hazard `count_clip_tokens.py` names in its own "WHAT MUST STAY IN SYNC"
note. The counted call needs a Part without issuing a request, which production
offers no way to obtain, so it reuses that script's `video_part` rather than
adding a third copy. The measurement is only worth anything if both halves
describe the request Phase 5 actually makes.

Usage:
    TT_CLIP_MODEL=gemini-2.5-pro uv run python scripts/diagnose_clip_token_gap.py \
        --project table-talk-497020 \
        --dataset table_talk_dev \
        --videos-bucket table-talk-497020-videos-dev \
        --location us-east1
"""

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

from google import genai
from google.cloud import bigquery
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from count_clip_tokens import video_part  # noqa: E402

from table_talk.gemini_caller import (  # noqa: E402
    CLIP_MODEL,
    GeminiPermanentError,
    GeminiTransientError,
    call_gemini_for_clip,
)
from table_talk.prompt_context import (  # noqa: E402
    build_action_context,
    build_fva_context,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"

# gemini_caller's own default, and the only location any production caller uses
# today. Overridable because `global` fans a request out to an unknown backend,
# which is the suspected source of the intermittency above -- a regional
# endpoint is the only way to hold that variable still.
DEFAULT_LOCATION = "global"

# Step D's user turn, verbatim from hand_action_processing.
STEP_D_USER_TEXT = "Extract the complete voluntary action sequence from this video clip."

# (video_id, hand_setup_time_seconds) for a real stored hand whose step D window
# is 20 s. Keyed by timestamp rather than by hand_start_id for the reason
# count_clip_tokens.py gives: ids are positional and renumber whenever a clip's
# detection count changes, so the timestamp is the stable anchor. Overridable so
# the audio rate below can be checked against a second video — one video alone
# cannot distinguish a rate from a constant that happens to match it.
DEFAULT_TARGET = ("YzKyFMQ1avU", 3330)

# The two deltas measured in the 2026-10-02 run, for placing this one.
MEASURED_DELTAS = (120836, 125490)

# Measured on YzKyFMQ1avU: prompt_audio was 128,675 on a 20 s window, and
# 128,675 / 5,147 s of video duration is exactly this. Printed against each
# run's own quotient so a second video either reproduces it or does not.
AUDIO_TOKENS_PER_SECOND = 25.0

# What a frame costs at the clip caller's fps=1.0 and unspecified media
# resolution, measured the same way (prompt_video 5,160 over a 20 s window).
VIDEO_TOKENS_PER_FRAME = 258


def fetch_target(
    client: bigquery.Client,
    project_id: str,
    dataset: str,
    video_id: str,
    hand_setup_time: int,
):
    """The target hand with the window the pending query would derive for it.

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
            v.duration_seconds,
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
          w.raw_lead_gap_seconds,
          w.duration_seconds
        FROM `{project_id}.{dataset}.hand_starts` h
        INNER JOIN windowed w USING (hand_setup_id)
        WHERE h.video_id = @video_id
          AND w.hand_setup_time_seconds = @hand_setup_time_seconds
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("video_id", "STRING", video_id),
            bigquery.ScalarQueryParameter("hand_setup_time_seconds", "INT64", hand_setup_time),
        ]
    )
    rows = list(client.query(query, job_config=job_config).result())
    if len(rows) != 1:
        raise SystemExit(
            f"expected 1 hand at {video_id} t={hand_setup_time}, got {len(rows)} — "
            "re-derive the timestamp against hand_setups"
        )
    return rows[0]


def billed_prompt_tokens(
    prompt: str,
    video_gcs_uri: str,
    start: int,
    end: int,
    project_id: str,
    entity_id: str,
    location: str,
) -> tuple[dict[str, int], str]:
    """Issue the one billed call. Returns (parsed usage fields, the usage line).

    The counts are read back off gemini_caller's own stderr line rather than
    from the response, because the response never leaves the caller -- and
    reading the line is also what proves the line carries what this script
    claims it does. _log_usage runs before validation, so a response that fails
    to parse still reports what it consumed; the answer is irrelevant here, only
    the bill is.
    """
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            call_gemini_for_clip(
                prompt,
                video_gcs_uri,
                start,
                end,
                project_id,
                location,
                user_text=STEP_D_USER_TEXT,
                label="diagnose_clip_token_gap",
                entity_id=entity_id,
            )
    except (GeminiPermanentError, GeminiTransientError) as exc:
        print(f"(call did not return usable JSON, which does not affect the count: {exc})")

    line = next(
        (ln for ln in captured.getvalue().splitlines() if ln.startswith("gemini_usage ")),
        None,
    )
    if line is None:
        raise SystemExit(
            "no gemini_usage line was emitted — the response carried no "
            "usage_metadata, so there is nothing to compare"
        )
    fields = {}
    for field in line.split()[1:]:
        key, _, value = field.partition("=")
        if value.isdigit():
            fields[key] = int(value)
    return fields, line


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--videos-bucket", required=True)
    parser.add_argument("--video-id", default=DEFAULT_TARGET[0])
    parser.add_argument("--hand-setup-time", type=int, default=DEFAULT_TARGET[1])
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    args = parser.parse_args()

    # The measurement is model-specific and the call is billed, and the module
    # default is Flash -- so an unset env var would quietly measure the wrong
    # model and spend real money doing it. Refusing is cheaper than the rerun.
    if "TT_CLIP_MODEL" not in os.environ:
        raise SystemExit(
            "set TT_CLIP_MODEL explicitly (the run under investigation was "
            f"gemini-2.5-pro; this module would otherwise default to {CLIP_MODEL})"
        )

    actions_prompt = (PROMPTS_DIR / "extract_player_actions.md").read_text()

    bq = bigquery.Client(project=args.project)
    row = fetch_target(
        bq, args.project, args.dataset, args.video_id, args.hand_setup_time
    )

    window = row.raw_lead_gap_seconds
    start = row.hand_setup_time_seconds
    end = start + window
    video_gcs_uri = f"gs://{args.videos_bucket}/{row.video_id}.mp4"

    filled_prompt = (
        actions_prompt
        .replace("{player_context}", build_action_context(row.hand_start_state))
        .replace(
            "{fva_context}",
            build_fva_context(row.hand_start_state["fva"], row.fva_time_seconds),
        )
    )

    print(f"model={CLIP_MODEL} location={args.location}")
    print(
        f"{row.hand_start_id}  t={start}  window={window}s [{start} -> {end}]  "
        f"video duration={row.duration_seconds}s\n"
    )

    client = genai.Client(vertexai=True, project=args.project, location=args.location)

    def count(parts: list) -> int:
        return client.models.count_tokens(
            model=CLIP_MODEL,
            contents=types.Content(role="user", parts=parts),
            config=types.CountTokensConfig(system_instruction=filled_prompt),
        ).total_tokens

    text_only = count([types.Part(text=STEP_D_USER_TEXT)])
    counted = count([video_part(video_gcs_uri, start, end), types.Part(text=STEP_D_USER_TEXT)])
    print("CountTokens (free, unbilled)")
    print(f"  total      {counted:>9,}")
    print(f"  text only  {text_only:>9,}")
    print(f"  video      {counted - text_only:>9,}  ({(counted - text_only) / window:.1f} tok/s)\n")

    fields, line = billed_prompt_tokens(
        filled_prompt,
        video_gcs_uri,
        start,
        end,
        args.project,
        f"{row.video_id}_t{start}",
        args.location,
    )
    print("generate_content (billed) — gemini_caller's own usage line:")
    print(f"  {line}\n")

    billed = fields.get("prompt_tokens")
    if billed is None:
        raise SystemExit("usage line carried no prompt_tokens — nothing to compare")

    delta = billed - counted
    print(f"billed {billed:,} - counted {counted:,} = {delta:+,}  ({billed / counted:.2f}x)")
    print(
        f"the 2026-10-02 step D calls measured {MEASURED_DELTAS[0]:+,} and "
        f"{MEASURED_DELTAS[1]:+,} on windows of 201 s and 91 s"
    )

    breakdown = {
        key: value
        for key, value in fields.items()
        if key.startswith("prompt_") and key != "prompt_tokens"
    }
    if breakdown:
        parts = "  ".join(f"{k[len('prompt_'):]}={v:,}" for k, v in breakdown.items())
        print(f"input by modality: {parts}")
    else:
        print(
            "input by modality: not reported — this model/endpoint returns no "
            "prompt_tokens_details, so what the excess consists of stays open"
        )

    # Which of the two observed billings this call got. Compared by exact
    # equality rather than a tolerance: every measurement so far has landed on
    # 25 * seconds to the token, on both videos and in both variants, so a
    # third answer is a new finding and should be reported as one rather than
    # rounded into whichever of these it sits nearer.
    #
    # The rate is a gemini-2.5-pro measurement, so another model billing audio
    # at its own rate reads as "unrecognised" even when it windows correctly.
    # Hence the two quotients: whichever span the audio was measured over gives
    # a clean tokens/second, and that identifies the behaviour at any rate.
    audio = breakdown.get("prompt_audio")
    video = breakdown.get("prompt_video")
    if audio is not None:
        windowed = int(AUDIO_TOKENS_PER_SECOND * window)
        whole_file = int(AUDIO_TOKENS_PER_SECOND * row.duration_seconds)
        verdict = {windowed: "windowed", whole_file: "whole_file"}.get(audio, "unrecognised")
        print(
            f"\naudio {audio:,}  (windowed would be {windowed:,} = "
            f"{AUDIO_TOKENS_PER_SECOND:g}x{window}s; whole file {whole_file:,} = "
            f"{AUDIO_TOKENS_PER_SECOND:g}x{row.duration_seconds}s)"
        )
        print(
            f"  audio / window   {window:>5}s = {audio / window:>10.2f} tok/s\n"
            f"  audio / duration {row.duration_seconds:>5}s = "
            f"{audio / row.duration_seconds:>10.2f} tok/s"
            "   <- the clean one names the span billed"
        )
    if video is not None:
        # Frames have been windowed on every call measured. If this ever stops
        # agreeing, the offsets are being ignored wholesale and not just for
        # audio, which is a different bug.
        print(
            f"video {video:,} / window {window}s = {video / window:.1f} tok/frame "
            f"(expected {VIDEO_TOKENS_PER_FRAME} at fps=1.0)"
        )
    if audio is not None:
        # Last line and machine-readable on purpose: a series of runs is tallied
        # with `grep -o 'AUDIO_BILLING=.*' | sort | uniq -c`. Carries model as
        # well as location, because both are things a run series varies.
        print(
            f"AUDIO_BILLING={verdict} model={CLIP_MODEL} location={args.location} "
            f"audio={audio} billed={billed}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
