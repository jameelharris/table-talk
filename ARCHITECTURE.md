# Architecture

Pipeline stages, file organization, and failure handling for `table-talk`.

## Pipeline overview

The system ingests poker broadcast videos and progressively extracts structured hand data through a series of stages. Each stage operates on the output of the previous stage and writes to BigQuery + GCS.

```
YouTube URL
   ↓ [Phase 1: Video ingestion]
videos table + GCS .mp4 file
   ↓ [Phase 2: Clip materialization]
clip_manifest table (inventory of 240s windows)
   ↓ [Phase 3: Hand setup identification]
hand_setups table + clip_processing_attempts table + GCS frame .jpg files
   ↓ [later phases]
```

This document covers phases that have been built. Later phases will be added as they're designed.

## Phase 1: Video ingestion

Downloads a video from YouTube via yt-dlp, uploads it to GCS, writes metadata to BQ.

### Production files

- `cli.py` — `tt ingest` subcommand
- `ingest.py` — orchestration: `process_url`, `process_manifest`, `reconcile_url`
- `manifest.py` — manifest_manifest`) and `extract_video_id`
- `videos_fetcher.py` — yt-dlp wrapper, error classification
- `videos_uploader.py` — GCS upload wrapper
- `videos_writer.py` — `videos` table writes
- `video_ingestion_attempts_writer.py` — `video_ingestion_attempts` state machine writes

### Test files

- `test_ingest.py`
- `test_manifest.py`
- `test_videos_fetcher.py`
- `test_videos_uploader.py`
- `test_videos_writer.py`
- `test_video_ingestion_attempts_writer.py`

### BQ tables

- `videos` — inventory of ingested videos (immutable per row)
- `video_ingestion_attempts` — insert-only state machine, one row per ingestion attempt

### CLI

```
tt ingest --manifest corpus/videos.yaml --project P --dataset D --bucket B
```

### Failure handling

Per-URL failures are caught and recorded in `video_ingestion_attempts` with a status indicating the failure category. The latest attempt for a URL determines retry behavior:

- `complete` → skip on next run
- `failed_transient_predownload` → retry on next run
- `failedownload` → retry on next run
- `failed_terminal` → skip on next run (no retry)

Status classification happens in `videos_fetcher.classify_error` based on the yt-dlp exception type and message.

## Phase 2: Clip materialization

Computes 240-second clip windows from a video's duration and writes the manifest to BQ. Does not produce video segments — only the inventory.

### Production files

- `cli.py` — `tt materialize-clips` subcommand
- `clip_materialization.py` — orchestration: `materialize_clips`, `materialize_clips_for_pending_videos`
- `clip_manifest_writer.py` — `clip_manifest` table writes (batched: one DML per video)

### Test files

- `test_clip_materialization.py`
- `test_clip_manifest_writer.py`

### BQ tables

- `clip_manifest` — inventory of clip windows per video (immutable per row)

### CLI

```
tt materialize-clips --project P --dataset D
tt materialize-clips --project P --dataset D --video-id VIDEO_ID
```

Without `--video-id`: materializes for all videos in `videos` that havifest` rows.
With `--video-id`: materializes only for the specified video; errors if the video isn't in `videos`.

### Failure handling

Per-video failures are logged to stdout only. No attempts table. Retry happens implicitly because failed videos still have no `clip_manifest` rows and remain pending on the next run.

This is intentionally simpler than Phase 1's per-URL attempt tracking. Materialization has narrower failure modes (no external dependencies — just BQ), and the failure cases that exist (invalid `duration_seconds`, missing video row) imply upstream bugs rather than transient errors. If materialization failure modes broaden, revisit this decision.

## Phase 3: Hand setup identification

Picks up pending clips from `clip_manifest`, downloads the source video to a per-video local tempfile, calls Gemini 2.5 Pro on each clip to identify hand setup moments, extracts a frame at each moment via ffmpeg, calls Gemini Pro on each frame to extract player info, enriches with deterministic seat metadata, d writes `hand_setups` rows + frame JPEGs to GCS. All work per clip is atomic — either all hand_setups rows for a clip land or none do. Defines hand setup moments only; per-hand action sequences are a later phase.

### Production files

- `cli.py` — `tt process-clips` subcommand
- `hand_setup_processing.py` — orchestration: `process_clip` (async, atomic per clip), `process_pending_clips` (per-video sequential outer loop with `asyncio.Semaphore(max_concurrent)` clip-level parallelism), `_find_pending_clips`, `_parse_timestamp`
- `videos_downloader.py` — GCS-to-local download (done once per video, reused across clips)
- `frame_extractor.py` — ffmpeg subprocess wrapper; sharpness/saturation filters match the notebook's image-quality settings
- `frame_uploader.py` — GCS frame upload
- `gemini_caller.py` — Vertex AI Gemini Pro caller (clip-mode video + frame-mode image), with truncated exponential backoff retry on `ResourceExhausted` (429): 5 attempts, full jitter, delays capped at 60s
- `hand_setu— `hand_setups` table writes (batched DML, JSON column passed as `dict` directly to `ScalarQueryParameter(type="JSON")` — single-encoded)
- `seat_enrichment.py` — deterministic `SEAT_NUMBER_MAP` (BB=1, SB=2, BTN=3, CO=4, HJ=5, LJ=6, UTG+2=7, UTG+1=8, UTG=9); `add_seat_numbers` injects + sorts players; `normalize_heads_up` rewrites SB→BTN when `total_seat_count == 2`
- `clip_processing_attempts_writer.py` — `clip_processing_attempts` state machine writes

### Test files

- `test_hand_setup_processing.py` (includes opt-in integration test against synthetic lavfi fixture)
- `test_videos_downloader.py`
- `test_frame_extractor.py`
- `test_frame_uploader.py`
- `test_gemini_caller.py` (includes retry-on-429 unit tests with patched sleep)
- `test_hand_setups_writer.py` (includes no-double-encoding regression test on wire value)
- `test_seat_enrichment.py`

### BQ tables

- `hand_setups` — one row per detected hand setup, scoped to clip; `hand_setup_state` is a JSON column with `total_seat_count`, `pot_s `players` (each enriched with `seat_number`)
- `clip_processing_attempts` — insert-only state machine, one row per processing attempt

### GCS

- `gs://table-talk-497020-hand-setups-dev/{video_id}/{clip_id}/{hand_setup_id}.jpg` — extracted frame at each hand setup's timestamp; deterministic path enables idempotent re-runs

### CLI

```
tt process-clips --project P --dataset D --videos-bucket VB --hand-setups-bucket HB [--video-id ID] [--max-concurrent 4]
```

### Failure handling

Per-clip failures are recorded in `clip_processing_attempts`. The latest attempt for a clip determines retry behavior:

- `complete` → skip
- `failed_transient` → retry on next run
- `failed_permanent` → skip (no retry)

Classification:

- Vertex 429 after retry exhaustion → `failed_transient` (most 429s never surface — they're absorbed silently by backoff)
- Other network / GCS / BQ errors → `failed_transient`
- Malformed JSON from LLM → `failed_permanent`
- LLM-returned timestamp outside `[clip_start_time, cliiled_permanent` (hallucination guard)
- LLM safety blocks → `failed_permanent`

Atomicity: within a clip, all per-hand-setup work (frame extraction, frame upload, frame-level Gemini call) happens before any BQ writes. The batch INSERT of all `hand_setups` rows and the `clip_processing_attempts` row both land in the same flow; if anything fails mid-clip, zero `hand_setups` rows persist for that clip.

## Cross-cutting

### Production files

- `cli.py` — entry point for all `tt` commands; one subcommand per phase
- `_generated/` — codegen output from `scripts/gen_schemas.py`, gitignored
- `prompts/*.md` — LLM prompts (currently `identify_hand.md`, `extract_player_info.md`), versioned with code so prompt changes ride code review

### Test files

- `test_smoke.py` — version sanity check

### Operational hardening

- GCS Data Access audit logs are enabled at the project level for `storage.googleapis.com` (ADMIN_READ + DATA_READ + DATA_WRITE) so object-level operations are traceable in Cloud Logging. Voll under the 50 GiB/month free tier for current workload.
- All GCS buckets have versioning + 7-day soft delete enabled; deletes are recoverable via `gcloud storage restore`.
- Integration tests must operate only on data they create (synthetic IDs, UUID-scoped paths). Any test that touches production-derived identifiers is treated as a bug.

## Known follow-ups

Not blocking any current phase, but accumulated as the project has grown:

- **CLI config defaults** — every command currently requires `--project --dataset --bucket`. Env var defaults (`TT_PROJECT`, etc.) would reduce friction.
- **`status_message` description typo** in `schemas/clip_processing_attempts.json` ("reason.NULL" missing a space).
- **CLI-layer testing gap** — `tt` commands have no automated tests. Deferred until something forces it.
- **`_bq_param_type` narrowness** — most writers handle only `str` and `int`. `hand_setups_writer` extended this to `dict` (JSON). Latent issue for any FLOAT64 / BOOL / BYTES column added later in otheers.
- **Hand-level deletion cascade tooling** — needed when downstream hand tables land.
- **`download_video` NotFound classification** — currently raises into `failed_transient` (loops forever). Should be `failed_permanent` for true 404s, since retry can't conjure the file back into existence.
- **CLI silent no-op on unknown `--video-id`** — if the filter matches zero pending clips, the command exits cleanly with `clips_processed: 0`. Should warn the operator that the filter matched nothing (typo? already complete?).
- **`status_message` truncation** in `clip_processing_attempts` writes — current 500-char limit can cut off ffmpeg / Gemini error details before the useful diagnostic content. Either raise the limit or extract the relevant tail.
- **Orphan frame cleanup on permanently-failed clips** — frames are uploaded before the batch INSERT; on permanent failure, the GCS frames persist without corresponding `hand_setups` rows. Self-healing on transient retry (deterministic paths); manual cleanupr permanent failures.
- **Integration test soft-delete cleanup** — synthetic `test_p3_*` fixtures accumulate in soft-delete after each integration run. Test `finally` block deletes the live version, but the noncurrent/soft-deleted copy lingers for 7 days. Cleanup could also purge from soft delete.
