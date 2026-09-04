# Architecture

Pipeline stages, file organization, and failure handling for `table-talk`.

CLAUDE.md's "Project Conventions" is the authoritative source for the rules themselves. This document describes where they are applied, and records the incidents that produced them.

## Pipeline overview

The system ingests poker broadcast videos and progressively extracts structured hand data through a series of stages. Each stage operates on the output of the previous stage and writes to BigQuery + GCS.

```
YouTube URL
   ↓ [Phase 1: Video ingestion]
videos table + GCS .mp4 file
   ↓ [Payout extraction]
tournament_results table + tournament_results_processing_attempts table + GCS frame .jpg file
   ↓ [Phase 2: Clip materialization]  ← GATED: a video with no tournament_results row is recorded blocked_upstream
clip_manifest table + clip_materialization_attempts table (inventory of 240s windows)
   ↓ [Phase 3: Hand setup identification]
hand_setups table + clip_processing_attempts table + GCS frame .jpg files
   ↓ [Phase 4: Hand start identification]
hand_starts table + hand_setup_processing_attempts table + GCS frame .jpg files
   ↓ [Phase 5: Player actions and community cards]
hand_actions table + hand_start_processing_attempts table + GCS frame .jpg files
   ↓ [assembly, then DBT validation]
```

`videos` is the only fact table. `clip_manifest`, `tournament_results`, `hand_setups`, `hand_starts`, and `hand_actions` are stage tables — a future assembly phase combines them into fact tables. The `*_attempts` tables are state tables. See CLAUDE.md's "Table taxonomy."

Phase numbers are historical labels reflecting the order phases were built, not a strict pipeline ordering. Payout extraction was designed after Phases 1–5 but runs second, so it carries no number rather than renumbering everything downstream of it; the same pragmatism as Phase 1's status vocabulary, where "the naming divergence is historical."

The arrow from payout extraction into Phase 2 is the pipeline's only hard dependency between phases: it is a gate, not merely an ordering. See "Admissibility: payout data gates the corpus."


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

## Payout extraction

Reads the tournament results panel from one early frame of a video and lands a single `tournament_results` row carrying the prize ladder and the tournament's bounty format.

Two things downstream needs that nothing else produces. ICM — the analytical premise of the project — is a function of the prize structure, not just the chip distribution, so without the ladder no ICM question is answerable. And Phase 3 selects a different extraction prompt for bounty events, which requires knowing whether the tournament is one. Both read off the same panel, so one Gemini call per video serves both.

The panel sits in the lower-right of the broadcast, is static for the whole video, and reads reliably. It is populated from the start of the replay — screenshot evidence shows a full nine-rank ladder while the chat still reads "Replay resumed" — which is why an early frame suffices.

This phase carries no number. See "Pipeline overview" on why.

### Production files

- `cli.py` — `tt extract-payouts` subcommand
- `payout_processing.py` — orchestration: `PendingVideo`, `_find_pending_videos`, `check_preconditions`, `_parse_amount`, `_normalize_panel`, `_validate_panel`, `_derive_bounty_type`, `_extract_with_fallback`, `_transient_status`, `_write_attempt`, `process_video`, `process_pending_videos`
- `tournament_results_writer.py` — `tournament_results` writes, replace semantics on `video_id`
- `tournament_results_processing_attempts_writer.py` — `tournament_results_processing_attempts` state writes
- `prompts/extract_results.md` — the single-frame panel read

Reused unchanged: `videos_downloader.py`, `frame_extractor.py`, `frame_uploader.py`, `gemini_caller.py`, `bq_utils.py`.

### Test files

- `test_payout_processing.py`
- `test_tournament_results_writer.py`
- `test_tournament_results_processing_attempts_writer.py`

### BQ tables

- `tournament_results` — stage table, one row per video. The first stage table whose upstream is a fact table rather than another stage table, so CLAUDE.md's "stage blobs nest their upstream verbatim" has nothing to nest: `videos` has no JSON column, and `tournament_results_state` is just `{"panel": {...}}`.
- `tournament_results_processing_attempts` — state table, one row per attempt

Every scalar column is `STRING` or `INT64`. All payout and bounty amounts are floats and live inside the JSON blob, which BigQuery parses server-side and which never reaches `bq_param_type` — the same reason every `hand_actions` column is REQUIRED and non-float. Do not add a `FLOAT64` column here without extending `bq_utils` first; codegen and Terraform both accept one and the failure surfaces only at the first write.

The state table is named after the phase's *output*, not its input. The house convention would give `video_processing_attempts`, which is uselessly generic beside Phase 1's `video_ingestion_attempts`. The deviation is deliberate.

### GCS

- `gs://table-talk-497020-tournament-results-dev/{video_id}/results.jpg` — the frame the results panel was read from; one per video, deterministic path so a re-run overwrites rather than orphaning

The ladder rung is deliberately **not** in the path. `frame_timestamp_seconds` already records which rung won, and a timestamped path would orphan the previous object every time a reprocess succeeded at a different rung. A stable path overwrites cleanly — the same property replace semantics give the BQ row.

The frame is uploaded after the panel validates and before the row is written. Validating first means a permanently-failing video leaves no object behind at all; uploading before the write preserves the one-directional guarantee that a live row's path always resolves.

**Retention was initially deferred, then revisited.** The first cut of this phase treated the frame as a tempfile on the grounds that nothing read it. That was wrong for one reason: `bounty_type` gates prompt selection for every hand in the video, and the frame is its only spot-check evidence. The reconciliation query below identifies *that* a video disagrees with its hands, not which side is wrong — and answering that without the frame means re-downloading 100–200 MB. One object per video is negligible against the ~15 GB the rest of the pipeline holds. This is the same reasoning already recorded under "Frames and orphaned GCS objects."

### CLI

```
tt extract-payouts --project P --dataset D --videos-bucket VB --tournament-results-bucket TRB [--video-id ID] [--max-attempts 3]
```

No `--max-concurrent`: one call per video, and the work is dominated by the download.

It must run before `tt process-clips`, which reads `bounty_type`. That ordering is enforced by Phase 3, not by this command, and `tt ingest` deliberately does not chain into it — chaining would entangle two independent failure modes and prevent re-running payout extraction alone after a prompt fix.

### The frame fallback ladder

`[5, 30, 120]` seconds. The panel is up from the start, but an early frame can land on a title card or a transition, so the ladder advances while `panel_visible` is false and fails permanently once exhausted. It costs nothing on the happy path: rung one succeeds and the rest are never extracted. The rung that succeeded is recorded in `frame_timestamp_seconds` and in `status_message`.

Rungs at or beyond the video's `duration_seconds` are skipped. Without that filter ffmpeg fails on every rung of a short video and the video retries to `failed_parked` for a condition that is deterministic and will never resolve.

### Preconditions

One check, and unlike Phases 4 and 5 it runs *before* the download. Those phases accept a post-download check because the download amortises across a video's many hands. Here the entity is the video, so there is nothing to amortise over — a skipping video would pay for its own full 100–200 MB download to produce zero calls — and `duration_seconds` comes from the pending query, so the check needs nothing the download provides. See "Preconditions run after the per-video download" under Cross-cutting.

A video no longer than the first ladder rung writes `complete_skipped`: it was examined and deliberately not processed, with zero LLM calls. That is a terminal success, not a failure — retrying would produce the same skip.

### Deriving `bounty_type`

From `has_bounty_column` alone. Knockout events print an extra Bounty column in the results panel; non-knockout events do not.

**The video title is not a usable signal.** `oWKpfjfEM4c` is a confirmed progressive-knockout event whose title does not say so, and YouTube metadata carries nothing else usable. The panel is the only reliable source, which is why this phase exists rather than a string match at ingest.

A corroborating second signal — seat bounty badges on the poker table in the same frame, cross-checked against the panel — was designed and dropped. This phase runs before Phase 3 and reads an arbitrary early frame with no guarantee the table is even on screen, so the check's most likely firing was a false positive on an unreadable table rather than a real contradiction, and its failure mode was a permanent park on a video that a later rung would have read fine.

The prompt compensates where that check would have helped: `panel_visible` is false if the panel is cut off at any edge or its full header row is not visible. A panel clipped on the right hides the Bounty column, and a hidden Bounty column is indistinguishable from an absent one — so such a frame fails rather than silently classifying a PKO as non-bounty.

`_validate_panel` enforces in code what the prompt can only ask for: a visible panel must carry at least `MIN_LADDER_RANKS` (5) rows and must include rank 1, or it was read partially. Both spike videos returned nine ranks; the floor is inferred from two videos and should be revisited as older or differently-branded broadcasts are ingested.

**Accepted risk.** One panel read drives prompt selection for every hand in the video, with no independent check. It is recoverable — append a retryable attempt row, re-run, and replace semantics overwrite the row — but it is not self-announcing. See "Reconciliation" below.

Freezeout (`static`) bounty is not modelled. No such event has been observed on this channel, and the project does not plan for formats it has not seen. A static event would show identical, unchanging badges at every seat, where a progressive event shows a halving lattice — observed at `MPBLfM4mwfE` as 125 → 187.5 → 281.25, spread across a factor of eleven. If one appears, `bounty_type` gains a value and a discriminator is added then.

### The asterisk is captured raw and interpreted nowhere

Some broadcasts prefix a payout with an asterisk. `payout_marked` records it; nothing in the pipeline reads it.

**What it means is not known.** On `YzKyFMQ1avU` ranks 1–3 are asterisked, and those are exactly the ranks whose payout ratios depart from the flat 1.425 that holds across ranks 4–9 — so the marker correlates with a departure from the published curve. That is a correlation on one video, not a meaning.

No geometric fitting, no schedule reconstruction, no `deal_detected` field. Those belong to downstream analysis, where a larger sample makes the meaning inferable rather than guessable from two videos.

Consequence to be aware of: if marked payouts are not the scheduled ladder, ICM computed on them measures the wrong environment. Downstream ICM work must either exclude marked videos or reconstruct a schedule, and that reconstruction's validity rests on a question that is currently open.

`_normalize_panel` also sets `payout_marked` when a raw payout string carries a leading asterisk. That fallback only fires on the string path — the prompt asks for bare numbers, so on the common path the asterisk never reaches Python. It is belt-and-braces against observed prompt variance (the spike saw the same prompt return `'$406.25'` on one frame and `406.25` on another), **not** a second source and not a cross-check on the model's own answer.

### Failure handling

- `complete` → skip on next run
- `complete_skipped` → skip; terminal success, precondition failed before any LLM call
- `failed_transient` → retry on next run (network, GCS, BQ, Vertex 429 after backoff exhausts, write errors)
- `failed_permanent` → skip; malformed JSON, safety block, GCS 404 on the video, panel not visible at any ladder rung, unreadable `currency_symbol`, non-boolean `has_bounty_column`, or a partially-read ladder
- `failed_parked` → skip; retry cap reached (default 3, `--max-attempts`)

The retry counter counts consecutive failures since the last non-failure, is computed in the pending query and carried on `PendingVideo`, and is never re-queried inside a failure handler — that handler runs precisely when BigQuery may be unreachable. The current failure is not yet counted, so the threshold test is `consecutive_failures + 1 >= max_attempts`.

Anything not recognised falls to the catch-all and is classified transient, including `GeminiTransientError`, which is deliberately not caught by name.

`_validate_panel` runs before the row is built. Every REQUIRED scalar column sourced from the model response must be non-null, because `bq_param_type` has no `None` branch and this phase's stage writer does not filter `None` out of the row dict — a null would raise `TypeError`, classify transient, and retry the video until it parks. Nulls *inside* `tournament_results_state` are fine: BigQuery parses that column server-side.

### Reconciliation

A wrong `bounty_type` is recoverable but not self-announcing, so it has to be looked for:

```sql
SELECT
  tr.video_id,
  tr.bounty_type,
  COUNT(hs.hand_setup_id) AS hand_setups,
  COUNTIF((
    SELECT COUNT(1)
    FROM UNNEST(JSON_QUERY_ARRAY(hs.hand_setup_state, '$.players')) p
    WHERE JSON_VALUE(p, '$.bounty') IS NOT NULL
  ) > 0) AS hand_setups_with_bounty_values
FROM `{project}.{dataset}.tournament_results` tr
LEFT JOIN `{project}.{dataset}.hand_setups` hs USING (video_id)
GROUP BY tr.video_id, tr.bounty_type
ORDER BY tr.video_id
```

A discrepancy is `bounty_type = 'progressive'` with zero bounty-bearing hands, or `bounty_type = 'none'` with more than zero. To correct one: append a retryable attempt row for the video, re-run `tt extract-payouts --video-id`, and the row is replaced.

Per-seat bounty extraction has landed, so the last column is meaningful for any video processed since. It still reads 0 for hands extracted before it — `MPBLfM4mwfE`'s existing 65 rows predate it and read 0 until that video is rebuilt (see "Rebuild, not backfill"). Read a zero on a `progressive` video as "not yet reprocessed" until the rebuild has run, and as a real discrepancy afterwards.

### Known inefficiency, accepted

Extracting one frame requires downloading the whole video from GCS — 100–200 MB for a ~50 minute broadcast — and it happens again when Phase 3 downloads the same video.

The alternatives both couple phases that are currently independent: extracting the frame during `tt ingest` while the file is already local, or seeking into a signed URL. Revisit if ingesting 150 videos makes the wall-clock cost real.

### Stated assumptions

**Panel layout generality rests on two videos.** Position, column structure and legibility held across two different broadcast skins (`MPBLfM4mwfE`, a progressive-knockout event, and `YzKyFMQ1avU`, a non-bounty event). That is enough to build against and not enough to claim for 150 videos. Expect to revisit as older or differently-branded videos are ingested; the `panel_visible` false path, the fallback ladder and `MIN_LADDER_RANKS` are the designed-in tolerance.

## Phase 2: Clip materialization

Computes 240-second clip windows from a video's duration and writes the manifest to BQ. Does not produce video segments — only the inventory.

### Production files

- `cli.py` — `tt materialize-clips` subcommand
- `clip_materialization.py` — orchestration: `materialize_clips`, `materialize_clips_for_pending_videos`, `_find_pending_videos`, `_find_video`, `_materialize_one`, `_payout_gate_reason`, `PendingVideo`
- `clip_manifest_writer.py` — `clip_manifest` table writes (batched: one replace DML per video)
- `clip_materialization_attempts_writer.py` — `clip_materialization_attempts` table writes

### Test files

- `test_clip_materialization.py`
- `test_clip_manifest_writer.py`
- `test_clip_materialization_attempts_writer.py`

### BQ tables

- `clip_manifest` — stage table, clip windows per video (replaced per video on rewrite)
- `clip_materialization_attempts` — state table, one row per materialization attempt

### CLI

```
tt materialize-clips --project P --dataset D [--max-attempts N]
tt materialize-clips --project P --dataset D --video-id VIDEO_ID [--max-attempts N]
```

`--max-attempts` defaults to 3, matching the other processing subcommands.

Without `--video-id`: materializes for all videos in `videos` whose latest `clip_materialization_attempts` status is absent, `failed_transient`, or `blocked_upstream`. Videos missing the payout row are named on stdout with the reason and recorded `blocked_upstream`; the run continues and exits zero. Prints the standard stats block: `videos_processed`, `videos_complete`, `videos_blocked_upstream`, `videos_failed_transient`, `videos_failed_permanent`, `videos_failed_parked`.
With `--video-id`: materializes the specified video **whatever its latest status** — deliberate reprocessing is the point of this entry point — and raises on any non-`complete` outcome, after recording it.

### Admissibility: payout data gates the corpus

A hand without payout context cannot participate in ICM analysis, and a corpus mixing hands that have it with hands that do not cannot be compared — pooled aggregates would be skewed and could support wrong conclusions. Payout data is therefore a **precondition for a hand being admissible to the corpus at all**, not a filter applied at query time.

That places the gate here rather than in Phase 3. If payouts define admissibility, the clips should never exist in the first place, and `clip_manifest` should never hold a row that ought not be processed. Phase 3's pending set is then correct by construction, which is how every other phase in this pipeline already works, and Phase 3 needs no check of its own.

The pending query drives off `videos` and LEFT JOINs `tournament_results` — not the reverse. `videos` is the fact table and the authoritative list of ingested videos; driving off the stage table would make a payout-less video *invisible* to the query rather than visible-and-skipped, and an invisible video cannot be reported. Reporting is the point: a silent no-op looks like success.

Both entry points enforce the gate, because admissibility cannot depend on which code path the operator took. They differ only in how they report. In the pending set a payout-less video is one of many not yet ready, so it is named and recorded `blocked_upstream` and the run continues. With `--video-id` the operator has asserted intent about one specific video, so the same row is written and then `MaterializeError` is raised — the same treatment as a video missing from `videos`.

Both read `has_payouts` from their own opening query, which is what makes each path safe on its own terms rather than by convention. An earlier design had the gate re-query inside a helper *after* an existing-clips early return, so that a video materialized before the gate existed would not start erroring. Both halves of that are gone: the early return was removed when Phase 2 gained a state table (see "Failure handling"), and the backfilled `complete` rows are what now keep already-materialized videos out of the pending set.

An earlier design put the check in `tt process-clips` and exited non-zero. Two problems, both real. `--video-id` is optional and the normal production invocation has no filter, so one terminally-failed payout extraction would have blocked every subsequent unscoped `process-clips` run corpus-wide. And it treated admissibility as a processing constraint rather than a property of the corpus, leaving `clip_manifest` holding rows that must never be processed and the pending query no longer the truth.

Consequences, accepted:

- **A video that fails payout extraction produces zero poker data**, permanently, until someone revisits it. That is the intent, not an oversight. Its action sequences and cards would have been valid for non-ICM questions, and they are discarded anyway, because a corpus that mixes the two cannot be compared.
- **Payout extraction reliability is now load-bearing for the whole corpus**, not just for ICM. It is proven on two videos. If the results panel is unreadable on some broadcast format, every video of that format is lost entirely. Revisit `FRAME_FALLBACK_LADDER` if failures cluster once ingesting at volume.

Diagnosis query for an operator asking why a video never materialized — never attempted, terminally failed, and parked all read differently:

```sql
SELECT v.video_id, a.status, a.status_message
FROM `{project}.{dataset}.videos` v
LEFT JOIN `{project}.{dataset}.tournament_results` tr USING (video_id)
LEFT JOIN (
  SELECT video_id, status, status_message,
         ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY attempted_at DESC) AS rn
  FROM `{project}.{dataset}.tournament_results_processing_attempts`
) a ON a.video_id = v.video_id AND a.rn = 1
WHERE tr.video_id IS NULL
```

### Failure handling

Phase 2 pairs `clip_manifest` with `clip_materialization_attempts`, like every other phase. Pending videos are those whose latest attempt status is absent, `failed_transient`, or `blocked_upstream`.

Five statuses:

| status | category | when |
|---|---|---|
| `complete` | terminal success | clips written |
| `blocked_upstream` | retryable, **not counted toward the cap** | no `tournament_results` row |
| `failed_transient` | retryable, counted | BQ or write error |
| `failed_permanent` | terminal failure | invalid `duration_seconds`, or no `videos` row |
| `failed_parked` | terminal failure | retry cap reached (`--max-attempts`, default 3) |

**A missing `tournament_results` row is neither an upstream bug nor a transient error** — it is a legitimate not-yet-ready state, resolved by running an upstream command. `blocked_upstream` records it: retryable, so the video is re-selected once payouts land, but deliberately outside the `failed%` prefix the consecutive-failure counter matches on, so a video behind a slow payout extraction cannot park for a condition that is not its fault. It is worded and tallied as a block rather than a failure, because a video awaiting payout extraction is a normal outcome of a correctly ordered pipeline and reporting it as an error would train operators to ignore it.

The Phase 2 row records the *consequence*; the cause stays durably recorded one phase upstream. That is why the message reads `tournament_results_processing_attempts` to distinguish never-attempted from `failed_transient` from a terminal `complete_skipped` / `failed_permanent` / `failed_parked` — the operator's next move differs in each case — and why the same reason is stored in `status_message` rather than only printed.

**Every path writes exactly one attempt row.** Both entry points funnel through one internal function that never raises and returns its status, which is what keeps the property from depending on which entry point ran. Note the direction of the risk: because the pending predicate is "absent or retryable," a video that writes no row keeps its prior status and is therefore still selected. A missed row does not strand a video — it stops the retry cap advancing, so a deterministically broken video would retry forever rather than parking.

`consecutive_failures` is computed in the opening query and carried on `PendingVideo`, never re-read in a failure handler. The `--video-id` path reads it the same way, up front, for the same reason: the handler runs precisely when BigQuery may be unreachable, so a read there would fail too and the natural fallback would silently defeat the cap for every video an outage touches.

#### Why this replaced the original no-attempts-table design

The exception was defensible when it was made: materialization had narrow failure modes and no external dependencies beyond BQ, its failure cases implied upstream bugs rather than transient errors, and retry fell out of a failed video still having no `clip_manifest` rows. Three things changed.

- **The payout gate** added a cross-phase dependency, and a state that is neither a bug nor a transient error, which the original two-category reasoning did not contemplate.
- **Scale.** One video failing silently among five is noticeable; among forty it is not. There was no record of which videos failed or how often, and no cap, so a video with a genuinely bad `duration_seconds` retried forever.
- **The cascade.** Marking a phase pending means appending a retryable attempt row. With no attempts table Phase 2 could not be marked, so a reprocessing cascade could not reach above Phase 3 — and a payout re-run, which changes `bounty_type` and therefore Phase 3's prompt, had no downstream state to mark at all.

Two consequences followed, and neither could land separately from the change.

**The existing-clips early return was removed.** It had quietly become load-bearing: it was the only thing preventing a duplicate write once pending-ness stopped deriving from output absence. But keeping it would have defeated the cascade entirely — marking a video pending does nothing if the run then skips every video that already has rows. Removing it is the point of the change, not a side effect of it.

**`clip_manifest` gained replace semantics**, keyed on `video_id`. Its single-batched-DML atomicity argument still holds; the "retry derives from the absence of output rows" half does not. A video with `blocked_upstream` or `failed_transient` is now re-selected *while it already has rows*, a state that could not previously arise, and a bare INSERT on top of them would produce duplicate clip windows for one video with every one of them feeding Phase 3. Phase 2 stopped being an exception in both ways at once: it gained a state table, and it now uses the same write pattern as every other stage.

Existing videos were materialized before the table existed, so `complete` rows were backfilled for them as a one-off. That is a stage-consistent record of what actually happened, not a rewrite of history, and it keeps the pending query clean rather than carrying a migration guard forever.

## Phase 3: Hand setup identification

Picks up pending clips from `clip_manifest`, downloads the source video to a per-video local tempfile, calls Gemini 2.5 Pro on each clip to identify hand setup moments, extracts a frame at each moment via ffmpeg, calls Gemini Pro on each frame to extract player info, enriches with deterministic seat metadata, and writes `hand_setups` rows + frame JPEGs to GCS. On progressive-knockout videos the frame prompt carries a bounty addendum and each player gains a `bounty`. All work per clip is atomic. Defines hand setup moments only; the first voluntary action is Phase 4 and per-hand action sequences are later.

### Production files

- `cli.py` — `tt process-clips` subcommand
- `hand_setup_processing.py` — orchestration: `PendingClip` (module-local frozen dataclass), `_find_pending_clips`, `process_clip` (async, atomic per clip), `process_pending_clips` (per-video sequential outer loop with `asyncio.Semaphore(max_concurrent)` clip-level parallelism), `_transient_status`, `_player_info_prompt`, `_parse_bounty`
- `prompts/extract_player_info_bounty_addendum.md` — appended to the base frame prompt when `bounty_type = 'progressive'`
- `videos_downloader.py` — GCS-to-local download (done once per video, reused across clips); raises `DownloadPermanentError` on GCS 404
- `frame_extractor.py` — ffmpeg subprocess wrapper; sharpness/saturation filters match the notebook's image-quality settings; accepts `float | int` timestamps
- `frame_uploader.py` — GCS frame upload
- `gemini_caller.py` — Vertex AI Gemini Pro caller (clip-mode video + frame-mode image), with truncated exponential backoff retry on HTTP 429: 5 attempts, full jitter, delays capped at 60s. The retry and the transient/permanent split key on the **status code** across both exception families the stack raises — `google.genai.errors.APIError` and `google.api_core.exceptions` — never on the exception class; see "The 429 backoff that never fired." `user_text` is required on both callers so no phase can silently inherit another's user turn.
- `hand_setups_writer.py` — `hand_setups` table writes (batched DML with replace semantics keyed on `clip_id`; JSON column passed as `dict` directly to `ScalarQueryParameter(type="JSON")` — single-encoded)
- `seat_enrichment.py` — deterministic `SEAT_NUMBER_MAP` (BB=1, SB=2, BTN=3, CO=4, HJ=5, LJ=6, UTG+2=7, UTG+1=8, UTG=9); `add_seat_numbers` injects + sorts players; `normalize_heads_up` rewrites SB→BTN when `total_seat_count == 2`
- `clip_processing_attempts_writer.py` — `clip_processing_attempts` state table writes
- `timestamp_utils.py` — `parse_timestamp`, shared with Phase 4
- `bq_utils.py` — `bq_param_type`, `build_replace_sql`, shared across writers

`_find_pending_clips` returns `PendingClip`, not the generated `ClipManifestRow`, because it carries a query-computed `consecutive_failures` that exists in no schema, plus `bounty_type` joined from `tournament_results`.

That join is LEFT, and a null `bounty_type` raises. The materialization gate guarantees no clip reaches `clip_manifest` without a payout row, so a missing one is a broken invariant rather than a data condition to degrade around. An INNER JOIN would encode the same assumption by silently dropping the clip — which is the failure shape the gate exists to prevent.

### Test files

- `test_hand_setup_processing.py` (includes opt-in integration tests against synthetic lavfi fixture and against the pending query)
- `test_videos_downloader.py`
- `test_frame_extractor.py`
- `test_frame_uploader.py`
- `test_gemini_caller.py` (includes retry-on-429 unit tests with patched sleep, covering **both** exception families — covering only `api_core` is what let the broken backoff ship green)
- `test_hand_setups_writer.py` (includes no-double-encoding regression test on wire value, and replace-semantics integration tests)
- `test_seat_enrichment.py`
- `test_timestamp_utils.py`
- `test_bq_utils.py`

### BQ tables

- `hand_setups` — stage table, one row per detected hand setup, scoped to clip; `hand_setup_state` is a JSON column with `total_seat_count`, `pot_size_bb`, `players` (each enriched with `seat_number`, and on progressive videos a `bounty`)
- `clip_processing_attempts` — state table, one row per processing attempt

### GCS

- `gs://table-talk-497020-hand-setups-dev/{video_id}/{clip_id}/{hand_setup_id}.jpg` — extracted frame at each hand setup's timestamp; deterministic path enables idempotent re-runs

### CLI

```
tt process-clips --project P --dataset D --videos-bucket VB --hand-setups-bucket HB [--video-id ID] [--max-concurrent 4] [--max-attempts 3]
```

### Per-seat bounty capture

On a progressive knockout each seat carries a badge below the avatar showing the live bounty on that player's head — what an opponent collects for eliminating them. It changes hand to hand as knockouts land, it is the term that makes calling correct in spots where ICM says fold, and nothing else in the pipeline derives it. The results panel's Bounty column that payout extraction captures is not a substitute: it reports realized collections by finishing rank and says nothing about what a player faced at the moment they acted.

`prompts/extract_player_info.md` is unchanged and is sent verbatim to non-bounty videos. For `bounty_type = 'progressive'` a separate fragment is concatenated at call time. Base-plus-addendum rather than two full prompt files, because two near-identical prompts drift and the shared body — the position-assignment procedure — is the part that matters.

**The addendum must ride the incumbent prompt; it cannot be a standalone call.** A standalone bounty probe read the values perfectly but mis-assigned them to seats, worsening with seat count: 4/4 agreement across four reps at three-handed, 2/4 at five-handed (two seats rotated), 3/4 at nine-handed with one rep fully reversed round the button. The values were identical every time; only the label-to-value mapping moved. That is the same misattribution recorded below for the rejected single-seat hole-card retry. A null bounty is a gap; a rotated one silently attributes the chip leader's bounty to a short stack. Riding the incumbent prompt fixes it because the seat is already determined by logic that has been correct corpus-wide.

Two details in the addendum are load-bearing and cost real debugging time:

- **The nesting instruction.** The frame response is wrapped in a `hand_setup` object. An earlier version said "as siblings of the existing fields" without naming the wrapper; the field landed at the wrong level and came back null every time.
- **The seat-binding sentence** — "the badge belonging to the player whose stack you reported for that seat" — is what fixes assignment, and is the likely cause of the output-token increase below. Do not reword it casually.

Cost, measured on `MPBLfM4mwfE_001_001` nine-handed over three reps each: prompt tokens 2,837 → 3,054 (+8%, the addendum text), total 4,831 → 6,589 (+36%), with output rising ~77% (≈1,994 → ≈3,535). Nine extra fields do not account for 1,500 tokens; that is reasoning about seat assignment. Roughly +$0.016 per hand setup, ≈$1.00 per video, ≈$26 across a 25-video PKO corpus. Small in absolute terms against a projected ~$1,060 for a 20K-hand corpus, but a large proportional jump on this one call — it is not free merely because no new call is made. Nine-handed is the worst case. A tighter phrasing of the seat-binding sentence may buy the same correctness for fewer tokens; optimise after it works, using the reproduce-before-changing harness.

`bounty` is parsed to a float by `_parse_bounty`, which strips `$` and `,`: the same prompt returned `'$406.25'` on one frame and `406.25` on another, so the addendum's "report it as a number" instruction alone is not sufficient. It is deliberately duplicated from `payout_processing._parse_amount` rather than shared — the duplication is a few lines and exact, the two can legitimately diverge (payout values carry a leading asterisk; badge values do not), and a shared module with one member is an abstraction for its own sake. Promote if a third caller appears.

The field is **absent, not null**, on non-bounty videos — nothing asked for it. Neither `schemas/hand_setups.json` nor codegen nor Terraform changes: `hand_setup_state` is a JSON column passed as a `dict`, so `bq_param_type` (which has no float branch) is never reached. Phase 4 nests `hand_setup_state` verbatim, so `bounty` propagates to `hand_starts` and `hand_actions` with no change there, and Phase 4's `check_preconditions` does not inspect it — a null bounty is a gap, not a skip.

**Reference images were considered and cut.** Phase 5's flop/turn/river references solve a *recognition* problem; this is not one — the spike read badge values 12/12 in isolation. They would add tokens to every per-hand call forever, create another code-to-prompt binding, and introduce a hallucination surface on concrete dollar values. They would only earn their place if one prompt had to serve both layouts, and the `bounty_type` gate means it does not.

`test_bounty_addendum_field_names_match_the_base_prompt` asserts that the keys the addendum references — `hand_setup`, `players`, `stack_size` — appear verbatim in `extract_player_info.md`. Like `test_reference_image_label_wording_matches_the_scan_prompt`, it is **not** an exception to "prompts have no automated tests": it asserts a code-to-prompt *interface* contract, not prompt content or quality. Renaming a key in the base would leave the addendum instructing the model to add a field alongside a `stack_size` that no longer exists, degrading extraction corpus-wide with no error anywhere. Do not delete it for violating a rule it does not violate.

Downstream must still *derive* rather than extract two things. Per-seat forced contributions: the ante coefficient is `(pot_size_bb − 1.5) / total_seat_count`, fitted per video by median so a misread pot does not drag it (`MPBLfM4mwfE` fits 0.125 exactly across seat counts 9 through 2). Note it is a **big blind ante** — the total collected is `0.125 × seats`, posted by one player, so `committed[seat]` is 0.5 for the SB, 1.0 plus the full table ante for the BB, and zero for everyone else; do not subtract 0.125 from every stack. And bounty equity: under progressive rules roughly half a collected bounty is banked and half added to the knocker's own head, so capturable equity is about half the displayed badge — inferred from a 125 → 187.5 → 281.25 progression, three data points, and worth confirming before anything depends on it.

**Currency mismatch, unresolved.** Badges and payouts are in dollars; stacks and pots are in big blinds. Nothing bridges them, and any equity computation needs that bridge. It connects to the open blind-level question and it blocks bounty-aware analysis.

**Stated assumptions.** Badge legibility and placement rest on one video — `MPBLfM4mwfE` is the only progressive event ingested, and `oWKpfjfEM4c` is a confirmed second PKO and the obvious next test of whether badge position holds across broadcast skins. And seat assignment is verified *stable*, not verified *correct*: three consistent reps rule out the rotation failure but cannot distinguish three right answers from three consistently wrong ones. The spot-check against the stored frame is the ground truth.

### Failure handling

Per-clip outcomes are recorded in `clip_processing_attempts`. The latest attempt determines retry behavior:

- `complete` → skip
- `failed_transient` → retry on next run
- `failed_permanent` → skip (no retry)
- `failed_parked` → skip (no retry); retry cap reached

Classification:

- Vertex 429 after retry exhaustion → `failed_transient` (see "The 429 backoff that never fired" — until that fix, *every* 429 surfaced)
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

- `complete` → skip; exactly one `hand_starts` row was written
- `complete_skipped` → skip (precondition failure, no LLM call made)
- `complete_uncontested` → skip (correct extraction, nothing to extract)
- `failed_transient` → retry on next run
- `failed_permanent` → skip (no retry)
- `failed_parked` → skip (no retry); retry cap reached

Three non-happy-path outcomes are distinguished deliberately, because they are not one bucket:

- **Uncontested** (`found: false`, `reason: "uncontested"`) — all fold to the BB, no voluntary chip commitment. A correctly-detected, stable poker outcome. Classified `complete_uncontested` with zero rows. Retrying would produce the same answer, and classifying it `failed_transient` would retry it forever. It carries its own status rather than sharing `complete` so that "this hand produced a row" is answerable from status alone, instead of resting on free text in `status_message`.
- **No first voluntary commitment found** (`found: false`, other reason) — a detection failure under the assumption that every hand setup has an identifiable hand start. `failed_transient`; a retry might succeed.
- **No second action found** (`found: true`, null `second_action_timestamp`) — likewise `failed_transient`.

The hallucination guard runs on the FVA timestamp *before* the second-action check, so a hallucinated FVA is classified `failed_permanent` even on a hand that also lacks a second action.

All three write zero `hand_starts` rows. The `complete_uncontested` branch calls the writer with an empty list, which clears any row from a prior run.

### What `hand_starts` guarantees, and what it does not

Established by exhaustive validation of 65 hands in video `MPBLfM4mwfE` (60 `complete` with rows, 1 `complete_uncontested`, 2 `complete_skipped`, 2 `failed_transient`; the uncontested hand predates the status and carries a historical `complete` row, superseded on reprocessing). The two `failed_transient` rows are now understood to be duplicate clip-boundary fragments of hands captured elsewhere in the corpus (see "Retry caps"), so the corpus holds roughly 63 distinct hands and there are no unexplained failures in it. Downstream phases must treat all of the following as **normal input**, not defects:

- **Eligible-seat hole cards may be `null`** in a `complete` record (4 of 65 observed). Most were on folded players and inconsequential. One was on a live player and is **frame-limited** — a six-time reproduction against the stored frame returned null every time while the adjacent seat read correctly, so the card is genuinely not legible at the FVA moment. More retries would not recover it.
- **Non-null hole cards may be wrong.** One hand returned `4d4s` for a seat holding `9s7d` — a hallucination, not a null. It landed on a folded seat, but the failure mode is real. `status_message` enumerates residual nulls and has **no signal** for wrong-but-non-null cards, so neither Phase 5 nor DBT can use status to filter bad data. The only mitigation would be consensus reads, which the pipeline does not do.
- **`complete` does not guarantee complete hole-card data.** Inspect `hand_start_state` for nulls on players who acted; do not rely on status.
- **`fva.action_type ∈ {call, raise, all_in}`.** No `limp` — it is derivable as `call` with `bet_amount == 1.0` preflop. `fold` and `check` are unreachable because the FVA is defined by chip commitment.

Whether a null or wrong card makes a hand unusable can only be judged against the assembled final hand state, which is downstream of Phase 5. So Phase 5 consumes possibly-null and possibly-wrong hole cards, tracks actions regardless, and DBT discards unusable hands. Observed consequential-error rate is roughly 3%.

### Hole-card retry

Step C sometimes returns null for a legible card. A single retry reuses the identical frame and prompt, fills gaps only, and never overwrites a non-null first answer. A narrower single-seat retry prompt was tried and rejected — it returned another player's cards mislabeled onto the retried seat.

The retry was built before the step-C stack-anchor prompt fix, which largely cured the null-hedging it was catching. It now fires rarely. If a future exhaustive run shows it effectively never fires, reconsider whether it earns its complexity.

## Phase 5: Player actions and community cards

Picks up pending rows from `hand_starts` and produces `hand_actions` rows carrying the complete voluntary action sequence by street, the community cards revealed on each postflop street with the timestamp they appeared, and the winning position(s). Populates no fact tables; `hand_actions` is a stage table for a future assembly phase.

Per hand start: check preconditions, run step D over the whole hand window for the action sequence and winning positions, then step E — for each postflop street D reported, a video scan for the reveal timestamp and a HIGH-resolution frame read for the new cards. E's results merge into D's street list, frames upload to GCS, and one `hand_actions` row is written.

### Production files

- `cli.py` — `tt process-hand-starts` subcommand
- `hand_action_processing.py` — orchestration: `PendingHandStart` (module-local frozen dataclass), `_find_pending_hand_starts`, `check_preconditions`, `_street_timestamp_guard`, `_scan_for_street`, `_read_street_cards`, `_transient_status`, `_write_attempt`, `process_hand_start` (async, atomic per hand start), `process_pending_hand_starts`
- `hand_actions_writer.py` — `hand_actions` table writes (replace semantics keyed on `hand_start_id`; `street_frame_gcs_paths` is REPEATED and goes through `ArrayQueryParameter`, never `None`)
- `hand_start_processing_attempts_writer.py` — `hand_start_processing_attempts` state table writes
- `reference_images.py` — `load_reference_images`, `STREET_REFERENCE_ORDER`, `reference_image_filename`
- `prompt_context.py` — extended with `build_action_context` (position, stack and hole cards per seat) and `build_prior_cards_context`
- `seat_enrichment.py` — extended with `heads_up_label`, the pure SB-is-BTN rule `normalize_heads_up` now rewrites through
- `prompts/extract_player_actions.md`, `prompts/extract_community_cards.md`, `prompts/extract_community_cards_from_frame.md`
- `references/{flop,turn,river}_reference.jpeg`

Shares `videos_downloader.py`, `frame_extractor.py`, `frame_uploader.py`, `gemini_caller.py`, `timestamp_utils.py`, `card_normalization.py`, and `bq_utils.py` with Phases 3 and 4.

### Test files

- `test_hand_action_processing.py` (includes opt-in integration tests)
- `test_hand_actions_writer.py`
- `test_hand_start_processing_attempts_writer.py`
- `test_reference_images.py`

Plus additions to `test_prompt_context.py`, `test_seat_enrichment.py`, and `test_gemini_caller.py`.

### BQ tables

- `hand_actions` — stage table, one row per hand start. `hand_action_state` is a JSON column shaped `{"hand_start": {...verbatim from hand_starts}, "streets": [{street_name, street_timestamp, community_cards, actions}], "winning_positions": [...]}`. There is **no `hand_action_id`**: the table is 1:1 with `hand_starts`, so `hand_start_id` is both the primary key and the natural key for replace-semantics writes. A separate id would have been byte-identical to `hand_start_id`, and a `{hand_start_id}_001` chain would compound a suffix per phase.
- `hand_start_processing_attempts` — state table, one row per processing attempt

### GCS

- `gs://table-talk-497020-hand-actions-dev/{video_id}/{clip_id}/{hand_setup_id}/{street}.jpg`

Named by street rather than index, so the path is self-describing. Up to three per hand; zero for a hand that ended preflop.

### CLI

```
tt process-hand-starts --project P --dataset D --videos-bucket VB --hand-actions-bucket AB [--video-id ID] [--hand-start-id ID] [--max-concurrent 4] [--max-attempts 3]
```

### D and E run sequentially

D is one video call over the whole hand window returning the action sequence by street plus `winning_positions`. E resolves each postflop street with a video scan for the reveal timestamp and a HIGH-resolution frame read for the new cards.

E could discover the streets itself — it self-terminates on `found: false` — but running the two concurrently costs real money for no throughput gain. A hand that ends preflop needs zero E calls; under parallelism a flop scan would already have fired. A transient D failure would waste up to six E calls. Throughput is already saturated by hand-level concurrency, so parallelising within a hand buys per-hand latency, which does not compound.

Within E the streets are sequential by necessity: each scan window starts at the previous street's timestamp, and each read's card count is keyed off the accumulated `prior_cards` (0 → 3, 3 → 1, 4 → 1).

### The processing window: skip, not truncate

The bound is the raw LEAD gap capped at `MAX_WINDOW_SECONDS` (240) — a technical ceiling, since a longer window at `fps=1.0` exceeds Gemini's 256-frame limit for a single video part.

Phase 4 truncates at its cap because it only needs the hand's start. Phase 5 needs the whole hand, so truncation would produce a record showing the hand ending early — a silent wrong answer rather than a loud failure. Because `raw_lead_gap_seconds` is known before any Gemini call, a hand exceeding the cap is a precondition skip instead.

A hand genuinely longer than 240 seconds therefore cannot be processed at 1 fps. The options — lower fps, or split the window across two calls and stitch — are real work and out of scope. Zero of 65 corpus hands exceed the cap (max gap 168s, median 34s), so the limit is currently theoretical and the cap guards against pathological gaps rather than filtering routinely.

### Two bounds on the window

`raw_lead_gap_seconds` measures the distance to the *next hand setup*, which is an upper bound on this hand's length rather than the length itself. So the pre-hoc cap has false positives, and more importantly cannot detect a window insufficient for any other reason.

`winning_positions` is the post-hoc companion. D derives it by observing which seat the pot is pushed toward, so an empty array means the window ended before the pot was awarded. The check runs immediately after D returns and before any E call, so a truncated window costs one call rather than seven. The operator prompt requires an unobserved award to return an empty array rather than a guess.

### Null community cards fail the hand

A null community card is categorically more serious than a null hole card: the board is shared, so one unreadable card invalidates the hand for every player. The frame read retries up to `CARD_READ_ATTEMPTS` (3); a residual null fails the hand `failed_transient` with the street named in `status_message`.

`_read_street_cards` raises rather than returning a null-bearing list, so `prior_cards` only ever grows by a fully-read street. The PoC filtered nulls out of the accumulator instead, which left the next read believing there were fewer prior cards than there were and broke the count rule. `_street_cards_unusable` also rejects a wrong-length read, since the null check alone passes an empty list.

### Reference images

Three universal JPEGs showing what flop, turn and river look like in a PokerStars broadcast, versioned with code in `references/`. `call_gemini_for_clip` emits a text part reading `Reference image — {label}:` before each blob, matching the wording `extract_community_cards.md` uses to describe them. That correspondence is what binds each description to its image; `test_reference_image_label_wording_matches_the_scan_prompt` asserts the rendered labels appear verbatim in the prompt file.

That test is **not** an exception to "prompts have no automated tests." It asserts a code-to-prompt *interface* contract, not prompt content or quality. Rewording either side would otherwise unbind the descriptions from the images silently, degrading street detection corpus-wide with no error anywhere. Do not delete it for violating a rule it does not violate.

Ordering comes from `STREET_REFERENCE_ORDER`, never a directory listing — `sorted()` yields flop, river, turn.

### Scan retries fire only on disagreement with D

`_read_street_cards` retried the frame read from the start, but the scan result was accepted on the first attempt — the cheap operation guarded, the expensive failure not. The two directions of scan error are also asymmetric: a wrong `found: true` is caught by `_street_timestamp_guard`, while a wrong `found: false` truncates the hand, looks legitimate, and had no guard at all.

`_scan_for_street` now asks once more on `found: false`. The guard is structural rather than conditional: it is called only from the loop over the streets D reported, so an ordinary end-of-hand `found: false` never reaches it. D's claim gates only whether to ask again, never what the answer is — if both scans say no, E's answer stands.

Motivated by `MPBLfM4mwfE_006_001_001`, an all-in runout where a turn was certainly dealt: D reported it, E's scan missed it, and the hand was silently short two streets. Reproduction against the real stored window found the street in 3 of 4 runs, so the miss is stochastic rather than a prompt defect. Later-street windows are also the narrowest in the phase, so this buys a retry where a missed street is most expensive and a call is cheapest.

### Failure handling

The same five statuses as Phase 4. Two things Phase 4 does not share:

- **Phase 5 has no successful zero-row outcome.** A hand ending preflop still has a preflop street, so `complete` always means exactly one row. Phase 4's uncontested branch has no counterpart here.
- **Failures never clear existing output.** The orchestrator never calls `write_hand_actions([])`. A stage row may legitimately coexist with a later `failed_transient` or `failed_parked` attempt.

A mid-hand `found: false` from a street scan is **not** a failure — it is how E learns the hand ended. The hand is truncated there, status `complete`, and the disagreement with D is recorded in `status_message`.

### What the corpus run established

A 60-hand run over `MPBLfM4mwfE`, in the same spirit as Phase 4's "What `hand_starts` guarantees."

- **60 of 60 hands complete.** The first run gave 56 complete, 3 rate-limited transients, and 1 hallucinated flop timestamp caught by `_street_timestamp_guard`. All four resolved on a rerun at `--max-concurrent 2` after the prompt and scan-retry fixes.
  - The 3 rate-limited transients are real — they are still in `hand_start_processing_attempts`, carrying `429 RESOURCE_EXHAUSTED` status messages. The *explanation* was wrong: lowering concurrency worked because it avoided provoking 429s, not because it gave the backoff room to work. No backoff ran. See "The 429 backoff that never fired."
- **Cost is roughly $0.053 per hand** — 2.49M tokens over 204 calls, about $3.20 for the video. Input dominates. Video scans run ~20K tokens, frame reads ~2.4K, and scan cost falls with each street as the window narrows.
- **One prompt finding.** D over-reported streets on hands that ended preflop: the instruction to record streets with empty action arrays read as applying whenever no postflop action occurred, rather than only when a called all-in means cards keep coming. Reproduced 4/4 before and after the fix.
- **Three data errors in the 65 `hand_setups` rows** — one all-null stack read, one phantom seat (`_009_005` declared four seats with a three-seat pot), and one misread pot (`_011_002`). Two were filtered by Phase 4's null-stack precondition; the third propagated harmlessly.
- **The 65-row corpus holds roughly 63 distinct poker hands**, since two rows are clip-boundary fragments. Any integrity check or validation run reasoning from row counts must not expect 65 `hand_actions` rows. The fragment rate is measured on one video only; a materially higher rate on a future video would make the decision not to add a fragment precondition worth revisiting.

## Cross-cutting

### Production files

- `cli.py` — entry point for all `tt` commands; one subcommand per phase, plus `check-integrity`
- `bq_utils.py` — `bq_param_type` and `build_replace_sql`, shared by writers
- `integrity.py` — the read-only corpus audit behind `tt check-integrity`
- `mark_pending.py` — the reprocessing cascade behind `tt mark-pending`
- `timestamp_utils.py` — `parse_timestamp`, shared by orchestrators
- `_generated/` — codegen output from `scripts/gen_schemas.py`, committed
- `prompts/*.md` — LLM prompts (`extract_results.md`, `identify_hand.md`, `extract_player_info.md`, `identify_hand_start.md`, `extract_hole_cards.md`), versioned with code so prompt changes ride code review

### Test files

- `test_smoke.py` — version sanity check
- `test_integrity.py` — the integrity checks
- `test_mark_pending.py` — the reprocessing cascade

### Idempotent stage writes

Stage-table writes use replace semantics: in one transaction, delete existing rows for the entity's natural key, then insert. See CLAUDE.md's "BQ writes use DML with parameterized queries" for the rules. This section records why.

**The incident.** On 2026-08-03, hand `MPBLfM4mwfE_006_004` completed at 18:47:31 and its `hand_starts` row landed. At 18:57:47 a BigQuery outage caused the client's job poll to fail — a ten-minute retry loop ending in `404 ... Job not found` — and the orchestrator's catch-all recorded `failed_transient` for work that had in fact succeeded. Because the pending query keys only on latest attempt status, the hand returned to the pending set, and the 19:06:26 retry appended a **second identical row**. It was cleaned by hand. Phase 3 had the same structural gap and was more exposed, since one clip produces many rows and duplicates propagate downstream.

Four hands in the same batch took the same outage; only this one duplicated, because only this one had already written its row when the poll failed.

**Why the attempts table cannot be the source of truth.** A state row records what the client *believed*. The outage made that belief wrong. Any write-time check against the attempts table inherits the same wrong belief and permits the duplicate anyway. The output table is the only authoritative record.

**Why a `NOT EXISTS` guard on the pending query is wrong.** Four outcomes are legitimately terminal with zero stage rows — Phase 3's "no hand setups detected," Phase 4's `complete_skipped`, Phase 4's `complete_uncontested`, and Phase 4's two zero-row `failed_transient` branches. An output-existence filter would mark them permanently pending.

**Consequences accepted.** Replace semantics make stage writes mutating DML, which BigQuery serializes per table (~2 concurrent, rest queued) rather than the fine-grained path available to INSERT-only writes. Safe at the current `max_concurrent=4` with videos processed sequentially; a real constraint if concurrency rises. A transaction aborted under contention surfaces as a write error, classifies retryable, and retries safely — the failure mode degrades into the mechanism.

**Implications for integrity checks.** A stage row can coexist with a latest attempt of `failed_transient` (via the outage path) or `failed_parked` (a hand that completed, later failed three times, and parked). Failures never delete existing output, so both states are legitimate and must not be flagged as anomalies. The converse inference is now available for Phase 4 but only in one direction: a latest `complete` does imply exactly one `hand_starts` row, because the zero-row terminal successes carry their own statuses (`complete_skipped`, `complete_uncontested`). That does not invert — a missing row does not imply a non-`complete` status, and a present row does not imply a `complete` one, per the preceding paragraph. It also does not generalise: other phases' `complete` still spans zero-row outcomes.

### Corpus integrity checks

`tt check-integrity` is a read-only audit across the corpus. It deletes nothing, writes nothing, and always exits zero — findings are not command failures, because an audit you hesitate to run is an audit you do not run. `integrity.py` holds the checks as pure query builders plus a runner; it consults BigQuery and returns findings, composing no primitives.

It is the complement of a scoped `--dry-run`, not an overlap. A dry-run answers "if I proceed, what changes?" for entities you named; this answers "is anything currently wrong?" for everything. A stale row from a reprocess weeks ago never surfaces in a dry-run unless that entity happens to be named again.

**Scope is correctness, not progress.** A video part-way through the pipeline is incomplete, not inconsistent — `YzKyFMQ1avU`'s 68 stage rows with no Phase 4 or 5 output report clean. Conflating unprocessed with wrong would make the tool noisy from its first run, which is how an audit tool gets ignored.

Three checks: orphaned stage rows (anti-join against the parent table), duplicate natural ids (`GROUP BY ... HAVING COUNT(*) > 1`, the invariant that caught the 2026-08-03 duplicate), and status versus row count. The third is per entity, never a count comparison — an aggregate delta of 3 at 150 videos is a research project, a named id is actionable, and the ~63-distinct-hands caveat above makes corpus row-count equality meaningless anyway.

**Arity differs by phase; there is no single "complete means one row" rule.** Phase 2's `complete` produces one clip per 240s window, so `>= 1`. Phase 3 has no invariant at all. Phases 4, 5 and payout extraction are exactly one. The report names the invariant per phase rather than implying one rule.

**Phase 3's absence from the status/row check is a property of the phase, not a gap in the tool.** A clip that legitimately detects zero hand setups is `complete` with zero rows, so row count cannot be predicted from status. The report states this rather than silently omitting the phase.

**Failure statuses are unconstrained everywhere,** and this falls out of omission rather than an exclusion clause: a status absent from a phase's arity map is simply not checked, and the failure statuses are never listed. This is the false positive most likely to be introduced, since it would fire on exactly the incident the tool exists to catch — see the outage path above. A contract test binds each arity map to its writer's `VALID_STATUSES`, so a new status that is neither constrained nor deliberately exempt fails loudly rather than silently going unchecked.

Each phase is anchored on its **input** table rather than its attempts table, which makes the stage-to-attempts off-by-one tractable: an attempts table sits between the entity a phase consumes and the rows it produces, and is named for the former. Two things follow. Every input table carries `video_id` while `clip_processing_attempts`, `hand_setup_processing_attempts` and `hand_start_processing_attempts` do not, so this is what makes `--video-id` scoping possible at all. And an attempt row whose entity no longer exists — Phase 3 re-detection shrinkage — drops out rather than being flagged, which is correct: state tables are append-only audit logs and a superseded entity's history is not an anomaly.

Phase 1 and `videos` are out of scope. Not because fact tables do not exist — `videos` plainly does — but because `video_ingestion_attempts` predates the common status vocabulary and `videos` has no parent, leaving a duplicate `video_id` as the only applicable check. `tournament_results` *is* covered by the duplicate and status/row checks, and skipped only in the orphan check, since its parent is `videos`.

**Deliberately not covered.** Anything that deletes — read-only is the design, and the no-delete-flag rule below is unchanged. Progress reporting, which is a different question with a different answer shape. GCS objects. And cross-phase content staleness, which nothing detects today; the eventual answer is a content hash of the upstream row stored downstream, making a mismatch a one-column comparison, but that is a schema change to two tables and its own piece of work.

The first run against the pre-rebuild corpus returns exactly one finding: `MPBLfM4mwfE_010_002`, `complete` with zero `hand_starts` rows. Its `status_message` reads "complete: uncontested — no voluntary chip commitment", written 2026-08-03 before `complete_uncontested` existed. It is a true reading of the current state, it resolves itself when the rebuild appends a superseding `complete_uncontested` row, and no legacy tolerance was added — permanently weakening a check to hide a temporary artifact is the wrong trade.

### Reprocessing and the mark-pending cascade

`tt mark-pending` is the sanctioned way to make a finished entity eligible for reprocessing. It appends a retryable attempt row to the named stage's state table and to every stage downstream of it, and deletes the downstream stage rows that reprocessing would otherwise leave stale. `mark_pending.py` holds the cascade; `--dry-run` prints the same plan the real run executes, from the same code path.

It replaces a hand-written INSERT. Deleting a stage table's rows does not cause a phase to re-run — pending queries key on latest attempt status, and an output-existence guard is rejected because several outcomes are legitimately terminal with zero rows — so reprocessing has always required appending an attempt row by hand, per phase, against ids the operator worked out themselves.

**The dependency graph is not the pipeline order.**

```
tournament_results --+
                     +--> hand_setups --> hand_starts --> hand_actions
clip_manifest -------+
```

Two stages feed `hand_setups` and neither feeds the other: materialization is arithmetic on `duration_seconds`, so re-reading the payout panel cannot change a clip window. `--stage tournament_results` therefore skips `clip_manifest` entirely. This is an explicit adjacency map rather than a position in the `PHASES` tuple, because deriving it positionally would reintroduce the link — and that is not merely wasteful: deleting `clip_manifest` destroys clip ids and briefly orphans `hand_setups`. A unit test guards it.

**Deletes are downstream-only; the named stage's own rows are never touched.** Replace semantics overwrite those on the next run, and a DELETE up front would leave an entity with zero rows if processing then failed — the same reasoning that puts the DELETE at write time inside a stage writer. Deletes run child-before-parent and before any mark, so a delete that fails partway leaves nothing pending against a partially-cleared downstream.

**Stages are named for what gets rebuilt**, which is the phase's output table, because that is how an operator thinks. Naming them by input would make `--stage videos` ambiguous between payout extraction and materialization. The consequence is an off-by-one against the attempts tables, which are named for the entity a phase *consumes* — `hand_setup_processing_attempts` belongs to the phase producing `hand_starts`. `mark_pending.py` reuses `integrity.PHASES` for exactly this reason: anchoring on the input table makes the off-by-one disappear in code rather than in prose. Every run also echoes its interpretation ("Rebuilding hand_starts. Marking 65 hand setups pending (Phase 4 consumes hand setups)"), so passing an id of the wrong kind fails visibly instead of silently marking nothing. The line names the entity rather than its table, because the input table is not what gets marked and is never modified — naming it there read as though it were about to be.

**Marks below the named stage are partly speculative, and this is expected.** `--stage hand_setups` appends `hand_setup_processing_attempts` rows for ids that re-detection may not reproduce. Harmless: every pending query joins to its input table, so an attempt row for a vanished entity selects nothing — the same property that makes `check-integrity` anchor on input tables.

**A mark costs one retry slot.** Marks are written as `failed_transient`, the only retryable status every phase shares. It is inside the `failed%` family, so a mark appended after a `complete` leaves the entity at `consecutive_failures = 1` and it gets `max_attempts - 1` real attempts before parking. Pass `--max-attempts 4` on a rebuild run if the full three matter. `blocked_upstream` was not reused: it exists only in Phase 2 and means something specific.

Marks carry `status_message = "mark-pending: rebuilding {stage}"`. That string is the only trace distinguishing a synthetic mark from a real failure, and it matters — an audit reader must not read a deliberate reprocess as a rate-limit incident.

**`--video-id` is mandatory, single, and there is no all-videos mode.** This makes terminal entities eligible again, which costs real money when the phases run; an accidental corpus-wide invocation at 150 videos would be a large unintended expense. `--id` and `--status` narrow within the video. No status is excluded by default: whether a `failed_permanent` is recoverable is context-dependent — a malformed-JSON response may well be fixed by a prompt change, while the two clip-boundary fragments can never complete — so the dry-run reports the status composition and the operator decides. An unnarrowed run scopes its deletes by `video_id` alone, which additionally sweeps orphaned downstream rows that an id list could never reach, since an orphan's parent is by definition not in the entity set.

`videos` is not a valid stage. Re-running Phase 1 is re-acquisition, not reprocessing: it re-downloads from YouTube, may get a different encode, and Phase 1's status vocabulary predates the common one. Replacing a source file is a delete-and-re-ingest operation, rare enough not to build for.

### Retry caps

Orchestrators park an entity after N consecutive failures rather than retrying forever. See CLAUDE.md's "Retry caps and terminal parking" for the rules.

Two corpus hands motivated this: `MPBLfM4mwfE_008_004`, which failed three times with `no_first_voluntary_commitment_found` including after the step-A prompt fix, and `MPBLfM4mwfE_004_008`, which failed once the same way, then twice with `no second action observed within window`. An earlier version of this section read these as two different failure classes and warned against diagnosing them as one pattern. That was wrong; **retracted**. A later query against `clip_manifest` and `hand_setups` found the real cause: both are the same clip-boundary re-detection artifact. `_008_004` begins at 1915s; the next `hand_setups` row, `_009_001`, begins at 1920s — exactly clip 009's `clip_start_time` — leaving `_008_004` a five-second LEAD window. `_004_008` begins at 959s; the next row, `_005_001`, begins at 960s — exactly clip 005's `clip_start_time` — leaving it a one-second window. Neither is a detection failure: `_004_008`'s real first voluntary commitment is at 967s with its second action at 971s (confirmed by manual video check), both outside its one-second window, so no retry and no prompt fix could ever have found them. Both are duplicate fragments of hands already captured under a different `hand_setup_id` — `_005_001` and `_009_001` respectively — and both of those completed with `hand_starts` rows. No hands were lost; see "Data and schema" for the upstream fix this points to.

The retry cap still did the right thing here — it stopped two entities that could not complete from retrying forever — but honestly, in this case that meant saving wasted calls on unprocessable fragments, not isolating genuinely hard hands. The accumulate-hard-hands-for-diagnosis rationale has no confirmed instances yet.

These failures are cheap: a step-A miss never reaches the HIGH-resolution hole-card call and writes no stage row, so failing fast on hard hands saves cost rather than losing data.

The counter is consecutive failures since the last non-failure, not lifetime — `MPBLfM4mwfE_006_004`'s history (`complete` → `failed_transient` → `complete`) is the case that makes the distinction necessary.

Capping the per-video download-failure branch means three consecutive failed downloads of one video park every entity in it: a video-level cause with entity-level effect and video-wide blast radius. Accepted.

### Preconditions run after the per-video download

In both Phase 4 and Phase 5, `check_preconditions` sits inside the per-hand function, downstream of the per-video download in the orchestrator. A hand that skips therefore still pays for its video to be fetched.

This is harmless because the download is amortised across a video's hands: a video with tens of hands, one or two of which skip, pays nothing extra. The "every hand in this video skips" case is an artifact of a test that seeds a single hand, and restructuring the orchestrator to optimise a scenario production does not produce would be speculative work. An integration test covering a precondition skip must therefore still upload a fixture video, or the download's 404 branch fires first and the precondition is never reached.

The amortisation argument is specific to per-hand phases. A phase whose entity *is* the video has nothing to amortise over, so payout extraction checks its precondition ahead of the download instead — a skipping video would otherwise pay for its own full 100–200 MB fetch to produce zero calls, and `duration_seconds` is already on the pending row.

### What `check_preconditions` is for

It tests presence, not plausibility: whether the phase can do its work with the given input, not whether the input is correct. `MPBLfM4mwfE_011_002` records `pot_size_bb = 0.38` where 1.88 is expected — a misread — but its stacks, seat count and hole cards are fine and Phase 4 processed it correctly. Rejecting it would have discarded a usable hand to protect a field nothing reads.

Validity belongs in the DBT layer, where a failed check is recomputable rather than terminal. A precondition failure is permanent by construction: it writes `complete_skipped` and the entity is never selected again.

### Cost instrumentation

Both `gemini_caller` functions emit one line per call to stderr — model, an optional caller-supplied label, and the prompt, candidate and total token counts. Grep with `gemini_usage`.

Logging happens *before* `_parse_and_validate`, because a response that fails validation — MAX_TOKENS, SAFETY, malformed JSON — is still a billed call, and those are exactly the ones whose cost would otherwise vanish.

### Frames and orphaned GCS objects

Frames are written to deterministic paths and read only through the paths stored on stage rows. Nothing in the pipeline discovers frames by scanning a bucket, so an unreferenced object is unreachable by construction and cannot corrupt anything downstream.

**Orphaned frames are accepted and are not cleaned.** No sweep, no cleanup tooling, no delete flag on any integrity checker. This closes the earlier "orphan frame cleanup on permanently-failed clips" follow-up as *won't fix*.

The guarantee is one-directional: a live row's path always resolves, because uploads precede the row write. What could break it is automated deletion — so the operative rule is not to build any. Bucket lifecycle rules must stay scoped to noncurrent versions; an age-based rule on current versions would delete referenced frames.

Orphans arise from: upload-before-write on permanent failure; Phase 3 batch shrinkage on reprocess (4 rows become 3, the surplus row's frame detaches); Phase 4's uncontested branch clearing a row whose frames remain; and integration-test residue.

Those are all path *abandonment*. Path *reuse* is a separate case and is not an orphan at all. Ids are positional — `{clip_id}_{NNN}` — so re-detection can reassign a `hand_setup_id` to a different moment and overwrite its frame, leaving a surviving downstream row's path resolving to an image of a different hand. Nothing is unreferenced, so no cleanup addresses it; it is a row-consistency problem, handled by `tt mark-pending`'s cascade delete rather than by anything in this section. `tt check-integrity` does not detect it either — an id that survives re-detection has a parent and a plausible row count. Keep the won't-fix on cleanup regardless: not building automated deletion is the only thing preserving the guarantee that a live row's path always resolves.

Payout extraction adds a narrower case — upload succeeds, the BQ write then fails — but it is self-healing where the others are not: its path is stable per video, so the next successful run overwrites the object rather than leaving a second one. It cannot orphan on a permanent failure at all, because the upload runs only after the panel validates.

Frames are also the manual spot-check evidence for validating extracted JSON against reality, which is a further reason not to delete them.

### Cross-phase reprocessing cascade (substantially closed)

Making Phase 3 idempotent makes reprocessing it *safe*, which makes it something an operator would actually do — which surfaced this gap.

If Phase 3 reprocesses a clip and the new result differs, `hand_setups` is correctly replaced. But `hand_setup_id` is positional (`{clip_id}_{NNN}`), so the same id can come to describe a *different moment*, or disappear entirely when the row count shrinks. Phase 4's existing `hand_starts` rows for those ids keep their `complete` attempts and are never re-triggered. They are not duplicated — they are silently stale, or orphaned against a `hand_setups` row that no longer exists. `hand_action_state` embeds a *snapshot* of `hand_start_state`, so the same reprocess leaves `hand_actions` stale three layers down rather than two.

**What `tt mark-pending` closes.** Reprocessing *through the tool* no longer leaves orphans or stale downstream rows: marking a stage deletes every downstream stage's rows for the affected entities and marks every downstream phase pending, so the next `tt process-*` run rebuilds them from the new upstream. See "Reprocessing and the mark-pending cascade."

**What remains open.** Two things. Reprocessing that bypasses the tool — a direct `tt process-clips --clip-id X` still replaces `hand_setups` and leaves Phase 4 and 5 untouched, exactly as before. And **content staleness**: an id that survives re-detection but comes to describe a different moment. There is no orphan, the counts reconcile, and `tt check-integrity` passes, because an id with a parent and a plausible row count looks correct from every angle the audit has. Nothing detects it today; see the follow-up under Tooling.

### Rebuild, not backfill

When a change adds a field to a stage blob, the corpus has to catch up. The rule, which matters at 25 videos and does not at one:

An **additive** change can in principle be backfilled at the phase that owns it. Downstream snapshots merely lack a field they never had, and no phase reads its inputs from a nested blob, so nothing is left disagreeing. A **modifying** change requires a full rebuild.

Per-seat bounty is additive, and a rebuild was still chosen — on economics, not necessity. The corpus is one video at ~$3.20 end to end. Building migration machinery to avoid that optimises against a constraint that does not exist, and the machinery would have to exist and be correct before it saved anything. The rebuild also sidesteps the cascade gap above: Phase 3's clip-mode detection call is stochastic, so a full reprocess can change the number or timing of detected setups, and truncating `hand_setups`, `hand_starts` and `hand_actions` first avoids both the orphan case and the silently-stale case entirely.

Re-detection being stochastic, the row counts are expected to move. Record the new ones; a difference is not a defect. A large move is itself a finding about detection stability.

### The 429 backoff that never fired

`gemini_caller._call_with_retry` shipped catching `google.api_core.exceptions.ResourceExhausted`. The client is `google-genai`, which raises `google.genai.errors.ClientError` with `code == 429` instead. The two families share no ancestry — `genai`'s `APIError` is rooted at plain `Exception` and is not even a `GoogleAPIError` — so the `except` never matched. The backoff loop never ran once.

Every 429 therefore escaped the caller uncaught, fell through the orchestrators' catch-all, and recorded `failed_transient`. Nothing was lost: replace semantics and the pending query cover it. But a whole run cycle was burned where a jittered five-second sleep would have done, and each one counted toward the retry cap.

The evidence is in the attempts tables, not the logs: nine 429s, six in `clip_processing_attempts` and three in `hand_start_processing_attempts`, each with a `status_message` beginning `429 RESOURCE_EXHAUSTED. {'error': ...}` — the `str(ClientError)` format, reachable only if the exception escaped classification. Zero were ever absorbed. (Grepping the root `*.log` files for `429` finds only token counts like `prompt_tokens=2429`.)

Two consequences beyond the retry, from the same root cause:

- **A `genai` 4xx classified transient.** `_PERMANENT_EXC` listed only `api_core` types, so a bad request or auth failure escaped uncaught too and retried three times before parking — with a status that misrepresented why. The attempts table is the audit record, and it was recording the wrong category.
- **`ServerError` landed on `failed_transient` correctly, but by accident** — via the catch-all rather than by classification.

**The fix keys on the status code, not the exception class.** `ClientError` covers every 4xx including 429, so mapping the class wholesale to permanent would have re-broken the retry, and would have made 429's retryability depend on `_call_with_retry` having filtered it out first. `_genai_status_code` reads `code` or `status_code` (the spelling has moved across SDK versions) and never parses the message — a message mentioning 429 for an unrelated reason must not trigger a retry. An unreadable status classifies transient, matching the "anything not recognised is transient" convention.

**The `api_core` tuples stay.** They are not dead: `google-cloud-storage` genuinely raises them, which is why `videos_downloader.py` is correct as written and was never affected. The two-family handling is deliberate, not redundancy to simplify away.

The lesson generalises past this module: the mocked tests were green throughout, because every one injected `api_exc.ResourceExhausted` by hand. A test that constructs the exception itself only ever proves the handler matches the test's own assumption. The one place the real error surface was reachable — the integration test — exercised only the happy path.

### Operational hardening

- GCS Data Access audit logs are enabled at the project level for `storage.googleapis.com` (ADMIN_READ + DATA_READ + DATA_WRITE) so object-level operations are traceable in Cloud Logging. Well under the 50 GiB/month free tier for current workload.
- All GCS buckets have versioning + 7-day soft delete enabled; deletes are recoverable via `gcloud storage restore`.
- Integration tests must operate only on data they create (synthetic IDs, UUID-scoped paths). Any test that touches production-derived identifiers is treated as a bug.

### Validation discipline

Prompts have no automated tests. Every prompt bug found so far was resolved by reproducing the exact failing Gemini call in a notebook against the real stored frame or video window *before* changing anything, and by looking at the actual frame rather than theorising. Several plausible hypotheses were discarded that way — including a broad "all-in FVA" theory that nine completed hands disproved. Reproduction against real content is the regression guard; use the same harness pattern for future prompt work.

The two parked-hand fragments (see "Retry caps") are the counterexample in the other direction: two hands presumed to be prompt or detection failures turned out to be a window-derivation artifact, found by querying the pending-query inputs — `hand_setup_time_seconds`, the LEAD target, and `clip_manifest` boundaries — rather than by looking at frames. The lesson generalizes: confirm the model was actually shown the thing it failed to find before concluding the prompt is at fault.

Happy-path validation for Phase 4 was a manual exhaustive CLI run across a full video, not an automated single-sample integration test. The integration tests prove the plumbing; the corpus run proves the extraction.

### BB denomination and chip conservation

`stack_size` and `pot_size_bb` are expressed in big blinds, so the same chips give a smaller number after a level increase and values are not comparable across levels. The broadcast displays no blind level, ante or level clock anywhere on screen, so the level is not observable and can only be inferred.

It can be inferred, though. Summing `stack_size` across players and adding `pot_size_bb` gives a total identical between consecutive `hand_setups` rows to within ±0.2%, across all 64 corpus pairs including seat-count changes from bust-outs. `stack_size` is recorded before forced contributions leave the seats, which is why the pot must be added back. Four pairs depart — at t=585, 1203, 1779 and 2409, roughly every ten minutes, which is a level clock. The distribution is sharply bimodal: same-level pairs at 1.000 ± 0.002, level changes at ≥ 1.14, a gap of about 70× the noise floor.

### Observed extraction errors

Errors cluster in card reading, not action tracking. **Action data has been correct in every hand examined**, across both the PoC and the dev corpus.

**Hole-card errors are suit errors.** Two were found by spot-check across 60 hands: `MPBLfM4mwfE_002_002_001` recorded `AsJc` for an actual `AcJc`, and `MPBLfM4mwfE_009_004_001` recorded `Ah7s` for `Ad7s`. Rank was correct both times, and the seats' screen positions differed, so location is not the cause. Both are the four-colour-deck confusions `extract_hole_cards.md` explicitly warns about — clubs/spades and diamonds/hearts.

Consequence depends entirely on the hand. `_002_002_001` ended preflop, so the wrong suit never collides with anything and the record stays useful. `_009_004_001`'s `Ah` also appears on the flop, correctly recorded — an impossible duplicate, and the hand is unusable.

### Manual correction is a sanctioned path

When DBT flags a duplicate card, the correction belongs in `hand_starts.hand_start_state`, where the bad card originates — not in `hand_actions`, whose copy is an embedded snapshot. Editing the snapshot would leave the two records disagreeing with the upstream one still wrong.

The workflow is: DBT flags, the operator corrects `hand_starts.hand_start_state`, runs `tt mark-pending --video-id V --stage hand_actions --id <that hand start>` to append the retryable attempt row, and re-runs Phase 5, whose replace semantics overwrite the stale row. This is the most likely everyday use of `mark-pending`; before it existed the attempt row was a hand-written INSERT.

This is the first sanctioned manual data path in the pipeline. A hand-corrected value is currently indistinguishable from an extracted one.

### Zero-action street extraction is a deferred cost lever

Community cards on streets with no voluntary action are analytically inert: once players are all-in the runout decides the winner, but no decisions remain to evaluate. Skipping E once a zero-action street is reached would cut roughly 29% of Phase 5's calls — about $300 on a projected 20K-hand corpus costing ~$1,060.

Not taken, and the reason is observability rather than completeness. E's scan is the only independent check on D's street list: `MPBLfM4mwfE_003_001_001` had D report a flop on a hand where everyone folded preflop, caught only because E searched and found none. On runout hands there is no betting context and nothing else to sanity-check against — precisely where a second observation is most valuable. Skipping would also make a genuine miss indistinguishable from a correctly short hand.

If cost ever forces it, the halves are separable: skip the frame *read* and keep the scan, preserving the cross-check and the timestamp.

## Known follow-ups

Not blocking any current phase, but accumulated as the project has grown.

### Tooling

- **Cross-phase content staleness is undetected** — the residual half of the reprocessing cascade gap, now that `tt mark-pending` has closed the orphan and stale-row halves. A `hand_setup_id` that survives re-detection at a shifted moment leaves downstream rows describing a different hand, with no orphan and reconciling counts. The eventual answer is a content hash of the upstream row stored downstream, making a mismatch a one-column comparison — a schema change to two tables. **Spike the cheaper option first:** `hand_start_state.hand_setup` is already a verbatim copy of the parent blob, so a `TO_JSON_STRING` comparison may detect this today with no schema change, if BigQuery's JSON normalisation is consistent enough to make equal blobs compare equal. Two queries answer it: one same-row comparison that should return no differences, and one cross-row that should return differences. A spike, not a spec.
- **Ruff baseline** — 189 errors across `src/`, `tests/` and `scripts/`, 164 of them E501 against the configured 100-char limit and the rest auto-fixable imports plus two decorative unused mocks. Ruff is not in CI, which is why they accumulated. With that many standing errors a new one is invisible.
- **Notebook reproduction harnesses are untracked** — `*.ipynb` is gitignored, while `jupyterlab`, `ipykernel` and `pillow` are dev dependencies precisely because CLAUDE.md's regression guard for prompts is notebook reproduction. The harnesses themselves are not versioned, so each investigation rebuilds them.

### CLI ergonomics

- **Derive bucket names from a single `--environment` flag** using the `{project}-{purpose}-{environment}` convention Terraform already encodes, instead of the current several flags that must agree. Supersedes the earlier "CLI config defaults" item.
- **CLI silent no-op on unknown `--video-id`** — if the filter matches zero pending rows, the command exits cleanly with a zero count. Should warn that the filter matched nothing.
- **CLI-layer testing gap** — `tt` commands have no automated tests. Deferred until something forces it.

### Data and schema

- **`_bq_param_type` narrowness** — `bq_param_type` handles `str`, `int`, and `dict`; REPEATED columns are handled separately via `ArrayQueryParameter` in `hand_starts_writer`. A `FLOAT64`, `BOOL`, or `BYTES` column would make this live. Note `bool` is a subclass of `int` and must be checked first if added.
- **`status_message` truncation** — the 500-char limit can cut off ffmpeg or Gemini error detail before the useful tail. Either raise the limit or extract the tail.
- **`status_message` description typo** in `schemas/clip_processing_attempts.json` ("reason.NULL" missing a space).
- **Phase 3 re-detects an in-progress hand at the start of the next clip** — a hand a few seconds old still matches the hand-setup criteria, so Phase 3 writes a second `hand_setups` row for the same poker hand just after a clip boundary. The duplicate collapses the earlier row's Phase 4 LEAD window (down to one and five seconds in the two corpus instances, see "Retry caps") and adds a duplicate hand to the corpus — two of `MPBLfM4mwfE`'s 65 `hand_setups` rows are such fragments. The fix belongs in Phase 3 or Phase 2: suppress detections in the first few seconds of a clip, or deduplicate across clip boundaries. Deduplication cannot key on matching state, since the duplicate rows carry different stack snapshots a second apart; proximity in time across a clip boundary is the only usable signal. The failure mode is worse downstream than upstream — Phase 4 degrades visibly, as a failure, while a phase that needs the whole hand would see the fragment as a hand that ended early, a silent wrong answer rather than a loud one.

### Cleanup

- **Noncurrent-version lifecycle rule on the hand-starts bucket** — versioning plus soft delete means every reprocess leaves noncurrent versions behind. Harmless at dev scale, real at corpus scale. Scope strictly to noncurrent versions.
- **In-place mutation of `hs.hand_setup_state`** — `normalize_heads_up` and the hole-card matching loop mutate the frozen `PendingHandSetup`'s dict field. Harmless today; a `copy.deepcopy` at assembly would make it clean when next touched.
- **Writer keyword inconsistency** — `project=` in some writers, `project_id=` in others. Inherited from Phases 1–3 and preserved per-template since. Normalize in a standalone refactor.
- **Integration test soft-delete cleanup** — synthetic fixtures accumulate in soft delete after each run. Test `finally` blocks delete the live version; the noncurrent copy lingers for 7 days.
- **`test_fetch_video_smoke` depends on third-party availability** — it hits a real YouTube video and fails when the network blocks it or the video disappears. Intermittent failures here train operators to ignore integration results.

### Experiments

- **`user_text` may be unnecessary** — both system prompts are self-contained and end with the instruction the user turn repeats. Test a media-only user turn; if responses are unaffected, drop the second part from both callers entirely.