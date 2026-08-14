# Architecture

Pipeline stages, file organization, and failure handling for `table-talk`.

CLAUDE.md's "Project Conventions" is the authoritative source for the rules themselves. This document describes where they are applied, and records the incidents that produced them.

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
   ↓ [Phase 4: Hand start identification]
hand_starts table + hand_setup_processing_attempts table + GCS frame .jpg files
   ↓ [later phases]
```

`videos` is the only fact table. `clip_manifest`, `hand_setups`, and `hand_starts` are stage tables — a future assembly phase combines them into fact tables. The `*_attempts` tables are state tables. See CLAUDE.md's "Table taxonomy."

This document covers phases that have been built. Later phases will be added as they're designed.

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

- `videos` — fact table, ingested videos (immutable per row)
- `video_ingestion_attempts` — state table, one row per ingestion attempt

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

Phase 1 predates the `failed_transient` / `failed_permanent` / `failed_parked` vocabulary used by Phases 3 and 4 and has no retry cap. Its statuses map onto the same three categories (see CLAUDE.md's "Attempt status semantics"); the naming divergence is historical.

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

- `clip_manifest` — stage table, clip windows per video (immutable per row)

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

Note that Phase 2's pending check consults the *output* table rather than an attempts table, which is the same principle the idempotent-write pattern applies elsewhere (see "Idempotent stage writes"). Its single batched DML per video means a partial write cannot happen, so it needs no replace wrapper.

## Phase 3: Hand setup identification

Picks up pending clips from `clip_manifest`, downloads the source video to a per-video local tempfile, calls Gemini 2.5 Pro on each clip to identify hand setup moments, extracts a frame at each moment via ffmpeg, calls Gemini Pro on each frame to extract player info, enriches with deterministic seat metadata, and writes `hand_setups` rows + frame JPEGs to GCS. All work per clip is atomic. Defines hand setup moments only; the first voluntary action is Phase 4 and per-hand action sequences are later.

### Production files

- `cli.py` — `tt process-clips` subcommand
- `hand_setup_processing.py` — orchestration: `PendingClip` (module-local frozen dataclass), `_find_pending_clips`, `process_clip` (async, atomic per clip), `process_pending_clips` (per-video sequential outer loop with `asyncio.Semaphore(max_concurrent)` clip-level parallelism), `_transient_status`
- `videos_downloader.py` — GCS-to-local download (done once per video, reused across clips); raises `DownloadPermanentError` on GCS 404
- `frame_extractor.py` — ffmpeg subprocess wrapper; sharpness/saturation filters match the notebook's image-quality settings; accepts `float | int` timestamps
- `frame_uploader.py` — GCS frame upload
- `gemini_caller.py` — Vertex AI Gemini Pro caller (clip-mode video + frame-mode image), with truncated exponential backoff retry on `ResourceExhausted` (429): 5 attempts, full jitter, delays capped at 60s. `user_text` is required on both callers so no phase can silently inherit another's user turn.
- `hand_setups_writer.py` — `hand_setups` table writes (batched DML with replace semantics keyed on `clip_id`; JSON column passed as `dict` directly to `ScalarQueryParameter(type="JSON")` — single-encoded)
- `seat_enrichment.py` — deterministic `SEAT_NUMBER_MAP` (BB=1, SB=2, BTN=3, CO=4, HJ=5, LJ=6, UTG+2=7, UTG+1=8, UTG=9); `add_seat_numbers` injects + sorts players; `normalize_heads_up` rewrites SB→BTN when `total_seat_count == 2`
- `clip_processing_attempts_writer.py` — `clip_processing_attempts` state table writes
- `timestamp_utils.py` — `parse_timestamp`, shared with Phase 4
- `bq_utils.py` — `bq_param_type`, `build_replace_sql`, shared across writers

`_find_pending_clips` returns `PendingClip`, not the generated `ClipManifestRow`, because it carries a query-computed `consecutive_failures` that exists in no schema.

### Test files

- `test_hand_setup_processing.py` (includes opt-in integration tests against synthetic lavfi fixture and against the pending query)
- `test_videos_downloader.py`
- `test_frame_extractor.py`
- `test_frame_uploader.py`
- `test_gemini_caller.py` (includes retry-on-429 unit tests with patched sleep)
- `test_hand_setups_writer.py` (includes no-double-encoding regression test on wire value, and replace-semantics integration tests)
- `test_seat_enrichment.py`
- `test_timestamp_utils.py`
- `test_bq_utils.py`

### BQ tables

- `hand_setups` — stage table, one row per detected hand setup, scoped to clip; `hand_setup_state` is a JSON column with `total_seat_count`, `pot_size_bb`, `players` (each enriched with `seat_number`)
- `clip_processing_attempts` — state table, one row per processing attempt

### GCS

- `gs://table-talk-497020-hand-setups-dev/{video_id}/{clip_id}/{hand_setup_id}.jpg` — extracted frame at each hand setup's timestamp; deterministic path enables idempotent re-runs

### CLI

```
tt process-clips --project P --dataset D --videos-bucket VB --hand-setups-bucket HB [--video-id ID] [--max-concurrent 4] [--max-attempts 3]
```

### Failure handling

Per-clip outcomes are recorded in `clip_processing_attempts`. The latest attempt determines retry behavior:

- `complete` → skip
- `failed_transient` → retry on next run
- `failed_permanent` → skip (no retry)
- `failed_parked` → skip (no retry); retry cap reached

Classification:

- Vertex 429 after retry exhaustion → `failed_transient` (most 429s never surface — they're absorbed silently by backoff)
- Other network / GCS / BQ errors → `failed_transient`
- Malformed JSON from LLM → `failed_permanent`
- LLM-returned timestamp outside `[clip_start_time, clip_end_time]` → `failed_permanent` (hallucination guard)
- LLM safety blocks → `failed_permanent`
- GCS 404 on the source video → `failed_permanent` for every clip in that video
- Any of the above transient cases, at the retry cap → `failed_parked`

Atomicity: within a clip, all per-hand-setup work (frame extraction, frame upload, frame-level Gemini call) happens before any BQ writes. The batch write of all `hand_setups` rows for the clip is a single replace transaction, so a reprocess overwrites the clip's prior rows rather than appending, and a mid-clip failure leaves the prior state untouched. A clip that legitimately detects zero hand setups still calls the writer with an empty list, which clears any rows from a prior run.

## Phase 4: Hand start identification

Picks up pending rows from `hand_setups` and produces `hand_starts` rows capturing the first voluntary action (FVA), the second-action timestamp, hole cards for eligible players, and visual verification frames. Populates no fact tables; `hand_starts` is a stage table for a future assembly phase.

Per hand setup: check preconditions, call Gemini on a bounded video window to find the FVA and the second action, then in parallel extract three verification frames after the second action and extract the FVA frame for a HIGH-resolution hole-card read. Hole cards are matched back onto players by `seat_position_label` and normalized (`10s` → `Ts`). All four frames upload to GCS before the single `hand_starts` row is written.

### Production files

- `cli.py` — `tt process-hand-setups` subcommand
- `hand_start_processing.py` — orchestration: `PendingHandSetup` (module-local frozen dataclass), `_find_pending_hand_setups`, `check_preconditions`, `_hallucination_guard`, `_transient_status`, `_write_attempt`, `process_hand_setup` (async, atomic per hand setup), `process_pending_hand_setups`
- `hand_starts_writer.py` — `hand_starts` table writes (replace semantics keyed on `hand_setup_id`; `verify_frame_gcs_paths` is REPEATED and goes through `ArrayQueryParameter`, never `None`)
- `hand_setup_processing_attempts_writer.py` — `hand_setup_processing_attempts` state table writes
- `card_normalization.py` — `normalize_card` / `normalize_cards`
- `prompt_context.py` — `build_player_context`, `build_hole_card_context` (stack-annotated seat lines for the step-C prompt)
- `seat_enrichment.py` — extended with `add_fva_seat_number`; `normalize_heads_up` gained an optional `fva` parameter
- `prompts/identify_hand_start.md`, `prompts/extract_hole_cards.md`

Shares `videos_downloader.py`, `frame_extractor.py`, `frame_uploader.py`, `gemini_caller.py`, `timestamp_utils.py`, and `bq_utils.py` with Phase 3.

### Test files

- `test_hand_start_processing.py` (includes opt-in integration tests)
- `test_hand_starts_writer.py`
- `test_hand_setup_processing_attempts_writer.py`
- `test_card_normalization.py`
- `test_prompt_context.py`

### BQ tables

- `hand_starts` — stage table, one row per hand setup that produced a hand start; `hand_start_state` is a JSON column shaped `{"hand_setup": {...}, "fva": {seat_position_label, seat_number, action_type, bet_amount}}`
- `hand_setup_processing_attempts` — state table, one row per processing attempt

### GCS

- `gs://table-talk-497020-hand-starts-dev/{video_id}/{clip_id}/{hand_setup_id}/fva.jpg`
- `gs://table-talk-497020-hand-starts-dev/{video_id}/{clip_id}/{hand_setup_id}/verify_{n:03d}.jpg` (three frames at +0.05s, +0.10s, +0.15s after the second action)

### CLI

```
tt process-hand-setups --project P --dataset D --videos-bucket VB --hand-starts-bucket HB [--video-id ID] [--max-concurrent 4] [--max-attempts 3]
```

### The processing window: LEAD + cap

`hand_setups` rows record when a hand begins but not when it ends. The pending query derives each hand's window with `LEAD(hand_setup_time_seconds) OVER (PARTITION BY video_id ORDER BY hand_setup_time_seconds, hand_setup_id)`, falling back to the video's `duration_seconds` for the last hand, then caps the result at `MAX_AVAILABLE_SECONDS` (60).

The cap matters because a gap can be arbitrarily long — a break in play, a missed detection — and an uncapped window would send a very long segment to Gemini for a moment that occurs in the first few seconds. Both the raw gap and the capped value are carried on `PendingHandSetup`, so `status_message` can record when a cap applied.

### Preconditions and `complete_skipped`

`check_preconditions` runs before any LLM call and returns a skip reason if `hand_setup_state` cannot support hand-start processing: any player with a null `stack_size`, any player with a null `seat_position_label`, `total_seat_count < 2`, or a zero/null `pot_size_bb`. Checks run in order and the first failure wins.

A skip writes a `complete_skipped` attempt and zero `hand_starts` rows. It is a success, not a failure: the input is what it is, and retrying would skip again.

The null-stack check deliberately skips the **whole hand** when any single player has a null stack, rather than degrading to per-seat handling. Stacks are the per-seat anchor the hole-card prompt uses to identify seats, so a null stack means that seat's extraction context is unreliable. Whole-hand skip is preferred for simplicity, at an accepted small yield loss. Do not loosen this to per-seat handling without revisiting the hole-card prompt. A single-seat null stack also points at a per-seat gap in Phase 3's stack extraction for that frame.

### Failure handling

- `complete` → skip
- `complete_skipped` → skip (precondition failure, no LLM call made)
- `failed_transient` → retry on next run
- `failed_permanent` → skip (no retry)
- `failed_parked` → skip (no retry); retry cap reached

Three non-happy-path outcomes are distinguished deliberately, because they are not one bucket:

- **Uncontested** (`found: false`, `reason: "uncontested"`) — all fold to the BB, no voluntary chip commitment. A correctly-detected, stable poker outcome. Classified `complete` with zero rows. Retrying would produce the same answer, and classifying it `failed_transient` would retry it forever.
- **No first voluntary commitment found** (`found: false`, other reason) — a detection failure under the assumption that every hand setup has an identifiable hand start. `failed_transient`; a retry might succeed.
- **No second action found** (`found: true`, null `second_action_timestamp`) — likewise `failed_transient`.

The hallucination guard runs on the FVA timestamp *before* the second-action check, so a hallucinated FVA is classified `failed_permanent` even on a hand that also lacks a second action.

All three write zero `hand_starts` rows. The uncontested branch calls the writer with an empty list, which clears any row from a prior run.

### What `hand_starts` guarantees, and what it does not

Established by exhaustive validation of 65 hands in video `MPBLfM4mwfE` (60 `complete` with rows, 1 uncontested, 2 `complete_skipped`, 2 `failed_transient`). The two `failed_transient` rows are now understood to be duplicate clip-boundary fragments of hands captured elsewhere in the corpus (see "Retry caps"), so the corpus holds roughly 63 distinct hands and there are no unexplained failures in it. Downstream phases must treat all of the following as **normal input**, not defects:

- **Eligible-seat hole cards may be `null`** in a `complete` record (4 of 65 observed). Most were on folded players and inconsequential. One was on a live player and is **frame-limited** — a six-time reproduction against the stored frame returned null every time while the adjacent seat read correctly, so the card is genuinely not legible at the FVA moment. More retries would not recover it.
- **Non-null hole cards may be wrong.** One hand returned `4d4s` for a seat holding `9s7d` — a hallucination, not a null. It landed on a folded seat, but the failure mode is real. `status_message` enumerates residual nulls and has **no signal** for wrong-but-non-null cards, so neither Phase 5 nor DBT can use status to filter bad data. The only mitigation would be consensus reads, which the pipeline does not do.
- **`complete` does not guarantee complete hole-card data.** Inspect `hand_start_state` for nulls on players who acted; do not rely on status.
- **`fva.action_type ∈ {call, raise, all_in}`.** No `limp` — it is derivable as `call` with `bet_amount == 1.0` preflop. `fold` and `check` are unreachable because the FVA is defined by chip commitment.

Whether a null or wrong card makes a hand unusable can only be judged against the assembled final hand state, which is downstream of Phase 5. So Phase 5 consumes possibly-null and possibly-wrong hole cards, tracks actions regardless, and DBT discards unusable hands. Observed consequential-error rate is roughly 3%.

### Hole-card retry

Step C sometimes returns null for a legible card. A single retry reuses the identical frame and prompt, fills gaps only, and never overwrites a non-null first answer. A narrower single-seat retry prompt was tried and rejected — it returned another player's cards mislabeled onto the retried seat.

The retry was built before the step-C stack-anchor prompt fix, which largely cured the null-hedging it was catching. It now fires rarely. If a future exhaustive run shows it effectively never fires, reconsider whether it earns its complexity.

## Cross-cutting

### Production files

- `cli.py` — entry point for all `tt` commands; one subcommand per phase
- `bq_utils.py` — `bq_param_type` and `build_replace_sql`, shared by writers
- `timestamp_utils.py` — `parse_timestamp`, shared by orchestrators
- `_generated/` — codegen output from `scripts/gen_schemas.py`, committed
- `prompts/*.md` — LLM prompts (`identify_hand.md`, `extract_player_info.md`, `identify_hand_start.md`, `extract_hole_cards.md`), versioned with code so prompt changes ride code review

### Test files

- `test_smoke.py` — version sanity check

### Idempotent stage writes

Stage-table writes use replace semantics: in one transaction, delete existing rows for the entity's natural key, then insert. See CLAUDE.md's "BQ writes use DML with parameterized queries" for the rules. This section records why.

**The incident.** On 2026-08-03, hand `MPBLfM4mwfE_006_004` completed at 18:47:31 and its `hand_starts` row landed. At 18:57:47 a BigQuery outage caused the client's job poll to fail — a ten-minute retry loop ending in `404 ... Job not found` — and the orchestrator's catch-all recorded `failed_transient` for work that had in fact succeeded. Because the pending query keys only on latest attempt status, the hand returned to the pending set, and the 19:06:26 retry appended a **second identical row**. It was cleaned by hand. Phase 3 had the same structural gap and was more exposed, since one clip produces many rows and duplicates propagate downstream.

Four hands in the same batch took the same outage; only this one duplicated, because only this one had already written its row when the poll failed.

**Why the attempts table cannot be the source of truth.** A state row records what the client *believed*. The outage made that belief wrong. Any write-time check against the attempts table inherits the same wrong belief and permits the duplicate anyway. The output table is the only authoritative record.

**Why a `NOT EXISTS` guard on the pending query is wrong.** Four outcomes are legitimately terminal with zero stage rows — Phase 3's "no hand setups detected," Phase 4's `complete_skipped`, Phase 4's uncontested branch, and Phase 4's two zero-row `failed_transient` branches. An output-existence filter would mark them permanently pending.

**Consequences accepted.** Replace semantics make stage writes mutating DML, which BigQuery serializes per table (~2 concurrent, rest queued) rather than the fine-grained path available to INSERT-only writes. Safe at the current `max_concurrent=4` with videos processed sequentially; a real constraint if concurrency rises. A transaction aborted under contention surfaces as a write error, classifies retryable, and retries safely — the failure mode degrades into the mechanism.

**Implications for integrity checks.** A stage row can coexist with a latest attempt of `failed_transient` (via the outage path) or `failed_parked` (a hand that completed, later failed three times, and parked). Failures never delete existing output, so both states are legitimate and must not be flagged as anomalies. Conversely, a `complete` attempt does not imply a row exists — uncontested and skipped hands are `complete` with zero rows by design.

### Retry caps

Orchestrators park an entity after N consecutive failures rather than retrying forever. See CLAUDE.md's "Retry caps and terminal parking" for the rules.

Two corpus hands motivated this: `MPBLfM4mwfE_008_004`, which failed three times with `no_first_voluntary_commitment_found` including after the step-A prompt fix, and `MPBLfM4mwfE_004_008`, which failed once the same way, then twice with `no second action observed within window`. An earlier version of this section read these as two different failure classes and warned against diagnosing them as one pattern. That was wrong; **retracted**. A later query against `clip_manifest` and `hand_setups` found the real cause: both are the same clip-boundary re-detection artifact. `_008_004` begins at 1915s; the next `hand_setups` row, `_009_001`, begins at 1920s — exactly clip 009's `clip_start_time` — leaving `_008_004` a five-second LEAD window. `_004_008` begins at 959s; the next row, `_005_001`, begins at 960s — exactly clip 005's `clip_start_time` — leaving it a one-second window. Neither is a detection failure: `_004_008`'s real first voluntary commitment is at 967s with its second action at 971s (confirmed by manual video check), both outside its one-second window, so no retry and no prompt fix could ever have found them. Both are duplicate fragments of hands already captured under a different `hand_setup_id` — `_005_001` and `_009_001` respectively — and both of those completed with `hand_starts` rows. No hands were lost; see "Data and schema" for the upstream fix this points to.

The retry cap still did the right thing here — it stopped two entities that could not complete from retrying forever — but honestly, in this case that meant saving wasted calls on unprocessable fragments, not isolating genuinely hard hands. The accumulate-hard-hands-for-diagnosis rationale has no confirmed instances yet.

These failures are cheap: a step-A miss never reaches the HIGH-resolution hole-card call and writes no stage row, so failing fast on hard hands saves cost rather than losing data.

The counter is consecutive failures since the last non-failure, not lifetime — `MPBLfM4mwfE_006_004`'s history (`complete` → `failed_transient` → `complete`) is the case that makes the distinction necessary.

Capping the per-video download-failure branch means three consecutive failed downloads of one video park every entity in it: a video-level cause with entity-level effect and video-wide blast radius. Accepted.

### Frames and orphaned GCS objects

Frames are written to deterministic paths and read only through the paths stored on stage rows. Nothing in the pipeline discovers frames by scanning a bucket, so an unreferenced object is unreachable by construction and cannot corrupt anything downstream.

**Orphaned frames are accepted and are not cleaned.** No sweep, no cleanup tooling, no delete flag on any integrity checker. This closes the earlier "orphan frame cleanup on permanently-failed clips" follow-up as *won't fix*.

The guarantee is one-directional: a live row's path always resolves, because uploads precede the row write. What could break it is automated deletion — so the operative rule is not to build any. Bucket lifecycle rules must stay scoped to noncurrent versions; an age-based rule on current versions would delete referenced frames.

Orphans arise from: upload-before-write on permanent failure; Phase 3 batch shrinkage on reprocess (4 rows become 3, the surplus row's frame detaches); Phase 4's uncontested branch clearing a row whose frames remain; and integration-test residue.

Frames are also the manual spot-check evidence for validating extracted JSON against reality, which is a further reason not to delete them.

### Cross-phase reprocessing cascade (known gap)

Making Phase 3 idempotent makes reprocessing it *safe*, which makes it something an operator would actually do — which surfaces a gap.

If Phase 3 reprocesses a clip and the new result differs, `hand_setups` is correctly replaced. But `hand_setup_id` is positional (`{clip_id}_{NNN}`), so the same id can come to describe a *different moment*, or disappear entirely when the row count shrinks. Phase 4's existing `hand_starts` rows for those ids keep their `complete` attempts and are never re-triggered. They are not duplicated — they are silently stale, or orphaned against a `hand_setups` row that no longer exists.

There is no cascade tooling today. Phase 5 adds a third layer, which makes this worse before it makes it better; its design should account for cascade or explicitly defer it with a plan.

### Operational hardening

- GCS Data Access audit logs are enabled at the project level for `storage.googleapis.com` (ADMIN_READ + DATA_READ + DATA_WRITE) so object-level operations are traceable in Cloud Logging. Well under the 50 GiB/month free tier for current workload.
- All GCS buckets have versioning + 7-day soft delete enabled; deletes are recoverable via `gcloud storage restore`.
- Integration tests must operate only on data they create (synthetic IDs, UUID-scoped paths). Any test that touches production-derived identifiers is treated as a bug.

### Validation discipline

Prompts have no automated tests. Every prompt bug found so far was resolved by reproducing the exact failing Gemini call in a notebook against the real stored frame or video window *before* changing anything, and by looking at the actual frame rather than theorising. Several plausible hypotheses were discarded that way — including a broad "all-in FVA" theory that nine completed hands disproved. Reproduction against real content is the regression guard; use the same harness pattern for future prompt work.

The two parked-hand fragments (see "Retry caps") are the counterexample in the other direction: two hands presumed to be prompt or detection failures turned out to be a window-derivation artifact, found by querying the pending-query inputs — `hand_setup_time_seconds`, the LEAD target, and `clip_manifest` boundaries — rather than by looking at frames. The lesson generalizes: confirm the model was actually shown the thing it failed to find before concluding the prompt is at fault.

Happy-path validation for Phase 4 was a manual exhaustive CLI run across a full video, not an automated single-sample integration test. The integration tests prove the plumbing; the corpus run proves the extraction.

## Known follow-ups

Not blocking any current phase, but accumulated as the project has grown.

### Tooling

- **Standing integrity-check tool** — the invariant that caught the duplicate: `hand_starts` row count equals the count of complete-and-not-uncontested hands, and no duplicate natural id in either stage table. The uncontested exclusion is essential; a naive "complete count equals row count" check false-positives on them. Must also tolerate the row-exists-with-failed-status cases described under "Idempotent stage writes." Ship as a CLI subcommand or a documented query.
- **Hand-level deletion cascade tooling** — see "Cross-phase reprocessing cascade." Becomes acute when Phase 5's tables land.
- **Cost/token instrumentation** — the PoC tracked cost and token counts per Gemini call; production's `gemini_caller` returns only the parsed dict. Phase 4 is the most expensive phase (two calls per hand, one at HIGH resolution) and Phase 5 will likely be worse. Capture `usage_metadata` before corpus-scale runs.

### CLI ergonomics

- **Derive bucket names from a single `--environment` flag** using the `{project}-{purpose}-{environment}` convention Terraform already encodes, instead of the current several flags that must agree. Supersedes the earlier "CLI config defaults" item.
- **Expose row-level scoping** — `--clip-id` / `--hand-setup-id`, which the orchestrators already support via `only_clip_ids` / `only_hand_setup_ids` but neither CLI surfaces.
- **CLI silent no-op on unknown `--video-id`** — if the filter matches zero pending rows, the command exits cleanly with a zero count. Should warn that the filter matched nothing.
- **CLI-layer testing gap** — `tt` commands have no automated tests. Deferred until something forces it.

### Data and schema

- **`_bq_param_type` narrowness** — `bq_param_type` handles `str`, `int`, and `dict`; REPEATED columns are handled separately via `ArrayQueryParameter` in `hand_starts_writer`. A `FLOAT64`, `BOOL`, or `BYTES` column would make this live. Note `bool` is a subclass of `int` and must be checked first if added.
- **`status_message` truncation** — the 500-char limit can cut off ffmpeg or Gemini error detail before the useful tail. Either raise the limit or extract the tail.
- **`status_message` description typo** in `schemas/clip_processing_attempts.json` ("reason.NULL" missing a space).
- **Phase 3 re-detects an in-progress hand at the start of the next clip** — a hand a few seconds old still matches the hand-setup criteria, so Phase 3 writes a second `hand_setups` row for the same poker hand just after a clip boundary. The duplicate collapses the earlier row's Phase 4 LEAD window (down to one and five seconds in the two corpus instances, see "Retry caps") and adds a duplicate hand to the corpus — two of `MPBLfM4mwfE`'s 65 `hand_setups` rows are such fragments. The fix belongs in Phase 3 or Phase 2: suppress detections in the first few seconds of a clip, or deduplicate across clip boundaries. Deduplication cannot key on matching state, since the duplicate rows carry different stack snapshots a second apart; proximity in time across a clip boundary is the only usable signal. The failure mode is worse downstream than upstream — Phase 4 degrades visibly, as a failure, while a phase that needs the whole hand would see the fragment as a hand that ended early, a silent wrong answer rather than a loud one.

### Cleanup

- **Retire `build_fva_context`** — dead code since the step-C stack-anchor fix removed the `{fva_context}` substitution. Left in place with its passing test to keep that fix's diff scoped.
- **Noncurrent-version lifecycle rule on the hand-starts bucket** — versioning plus soft delete means every reprocess leaves noncurrent versions behind. Harmless at dev scale, real at corpus scale. Scope strictly to noncurrent versions.
- **In-place mutation of `hs.hand_setup_state`** — `normalize_heads_up` and the hole-card matching loop mutate the frozen `PendingHandSetup`'s dict field. Harmless today; a `copy.deepcopy` at assembly would make it clean when next touched.
- **Writer keyword inconsistency** — `project=` in some writers, `project_id=` in others. Inherited from Phases 1–3 and preserved per-template since. Normalize in a standalone refactor.
- **Integration test soft-delete cleanup** — synthetic fixtures accumulate in soft delete after each run. Test `finally` blocks delete the live version; the noncurrent copy lingers for 7 days.
- **`test_fetch_video_smoke` depends on third-party availability** — it hits a real YouTube video and fails when the network blocks it or the video disappears. Intermittent failures here train operators to ignore integration results.

### Experiments

- **`user_text` may be unnecessary** — both system prompts are self-contained and end with the instruction the user turn repeats. Test a media-only user turn; if responses are unaffected, drop the second part from both callers entirely.