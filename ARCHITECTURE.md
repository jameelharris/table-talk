# Architecture

Pipeline stages, file organization, and failure-handling conventions for `table-talk`.

## Pipeline overview

The system ingests poker broadcast videos and progressively extracts structured hand data through a series of stages. Each stage operates on the output of the previous stage and writes to BigQuery + GCS.

```
YouTube URL
   ↓ [Phase 1: Video ingestion]
videos table + GCS .mp4 file
   ↓ [Phase 2: Clip materialization]
clip_manifest table (inventory of 240s windows)
   ↓ [Phase 3: Hand setup identification]  ← not yet built
hand_setups table + clip_processing_attempts table
   ↓ [later phases]
```

This document covers phases that have been built (or are about to be built). Later phases will be added as they're designed.

## Phase 1: Video ingestion

Downloads a video from YouTube via yt-dlp, uploads it to GCS, writes metadata to BQ.

### Production files

- `cli.py` — `tt ingest` subcommand
- `ingest.py` — orchestration: `process_url`, `process_manifest`, `reconcile_url`
- `manifest.py` — YAML manifest loader (`load_manifest`) and `extract_video_id`
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
- `failed_transient_postdownload` → retry on next run
- `failed_terminal` → skip on next run (no retry)

Status classification happens in `videos_fetcher.classify_error` based on the yt-dlp exception type and message.

## Phase 2: Clip materialization

Computes 240-second clip windows from a video's duration and writes the manifest to BQ. Does not produce video segments — only the inventory.

### Production files

- `cli.py` — `tt materialize-clips` subcommand (shared with Phase 1)
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

Without `--video-id`: materializes for all videos in `videos` that have no `clip_manifest` rows.
With `--video-id`: materializes only for the specified video; errors if the video isn't in `videos`.

### Failure handling

Per-video failures are logged to stdout only. No attempts table. Retry happens implicitly because failed videos still have no `clip_manifest` rows and remain pending on the next run.

This is intentionally simpler than Phase 1's per-URL attempt tracking. Materialization has narrower failure modes (no external dependencies — just BQ), and the failure cases that exist (invalid `duration_seconds`, missing video row) imply upstream bugs rather than transient errors. If materialization failure modes broaden, revisit this decision.

## Phase 3: Hand setup identification (not yet built)

Picks up pending clips from `clip_manifest`, materializes the actual `.mp4` segment in GCS, runs the `identify_hand` LLM prompt against each clip, and writes structural output indicating whether the clip contains a hand setup. Does not define hand start times — that is a later phase.

### Production files (planned)

- `cli.py` — new subcommand (name TBD; `tt identify-hand-setups` or similar)
- TBD: clip processing orchestration
- TBD: GCS segment materialization (ffmpeg)
- TBD: Gemini caller
- TBD: `hand_setups_writer.py`
- `clip_processing_attempts_writer.py` — already exists, built in advance of this phase

### Test files (planned)

- Mirror the production file structure

### BQ tables

- `hand_setups` — structural output of hand setup identification (schema TBD, depends on `identify_hand` prompt output shape)
- `clip_processing_attempts` — insert-only state machine, one row per processing attempt (already exists)

### GCS

- `gs://table-talk-497020-hand-setups/` (or similar — to be provisioned)

### Failure handling

Per-clip failures will be recorded in `clip_processing_attempts`. The latest attempt for a clip determines retry behavior, following the same pattern as Phase 1's `video_ingestion_attempts`:

- `complete` → skip
- `failed_transient` → retry
- `failed_permanent` → skip (no retry)

Failure categories anticipated:

- LLM rate limits → transient
- Network / GCS / BQ errors → transient
- Schema validation failures → permanent
- LLM refusal → permanent

## Cross-cutting files

These don't belong to a single phase:

### Production files

- `cli.py` — entry point for all `tt` commands; one subcommand per phase as needed
- `_generated/` — codegen output from `scripts/gen_schemas.py`, gitignored

### Test files

- `test_smoke.py` — version sanity check

## Conventions

These apply across all phases. Established in early phase work, expected to continue in later phases.

### Schema as source of truth

Every BQ table has a `schemas/{table_name}.json` file. `scripts/gen_schemas.py` produces dataclass row types from these schemas. Terraform reads the same schemas to provision tables. Drift between codegen output and Terraform is caught at integration-test time.

### Two-table pattern (inventory + state machine)

Each major entity has two tables:

- An **inventory table** — immutable per row, captures "this thing exists." Written once at entity creation.
- A **state machine table** — insert-only, captures "this is what happened during an attempt to process this thing." One row per attempt. Server-defaulted `attempted_at` timestamp.

The current state of an entity is derived: latest row in the attempts table (by `attempted_at`) for that entity. No in-flight states are stored; rows are only written after attempts conclude.

### Writers are hand-written per table

Each table has a writer module following a template (`videos_writer.py` for simple cases, `video_ingestion_attempts_writer.py` for cases with status validation). No shared base class. Some duplication is accepted for consistency.

### Batch DML for high-cardinality writes

When one operator action produces many rows for a single entity (like `clip_manifest`'s many-clips-per-video), the writer accepts a list and produces one atomic DML INSERT. For single-row-per-event writes (`videos`, `video_ingestion_attempts`, `clip_processing_attempts`), writers stay single-row.

### Integration test scoping

Functions that operate on "all matching X" must accept a scope-limiting parameter (e.g., `only_X_ids: list[str] | None = None`) so integration tests can constrain their blast radius to test-owned data. Production callers leave the parameter as None.

Codified in CLAUDE.md.

### CLI commands map to logical groups

Each `tt` subcommand does one logical group's work. Cross-group composition is via command sequence, not bundled commands. Each phase that needs operator-invoked work gets its own subcommand.

### INT64 seconds for video-offset times

All time offsets within a video (clip start/end, hand start within clip, etc.) are stored as INT64 seconds.

### Idempotence and self-healing

Processing functions should be idempotent on their inputs. Re-running a command should be safe and should naturally fill in any missing work. Idempotence checks are derived from the attempts tables ("does any attempt with status='complete' exist for this entity?").

## Known follow-ups

Not blocking any current phase, but accumulated as the project has grown:

- **CLI config defaults** — every command currently requires `--project --dataset --bucket`. Env var defaults (`TT_PROJECT`, etc.) would reduce friction.
- **`status_message` description typo** in `schemas/clip_processing_attempts.json` ("reason.NULL" missing a space).
- **CLI-layer testing gap** — `tt` commands have no automated tests. Deferred until something forces it.
- **`_bq_param_type` narrowness** — writers only handle `str` and `int` parameter types. Latent issue for any FLOAT64 / BOOL / BYTES column added later.
- **Hand-level deletion cascade tooling** — needed when downstream hand tables land.
