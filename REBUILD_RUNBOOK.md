# Rebuild runbook — phase gates, inert skip, provenance

Rebuilds both corpus videos from payout extraction down, so every row is
produced with every gate in place and carries provenance. Run per video.

Environment, used throughout:

```
PROJECT=table-talk-497020
DATASET=table_talk_dev
VIDEOS=table-talk-497020-videos-dev
RESULTS=table-talk-497020-tournament-results-dev
SETUPS=table-talk-497020-hand-setups-dev
STARTS=table-talk-497020-hand-starts-dev
ACTIONS=table-talk-497020-hand-actions-dev
```

Videos: `MPBLfM4mwfE`, `YzKyFMQ1avU`.

---

## Step 0 — Terraform

Six schema **descriptions** changed across this work: four for `provenance`
(Commit 1) and two on `hand_actions` for `extraction_status` and
`street_frame_gcs_paths` (Commit 5). No columns, types or modes changed, and
codegen output is byte-identical.

```
cd terraform/environments/dev && terraform plan
```

**Expect: in-place description updates only, and no table replacements.** If the
plan proposes destroying or recreating any table, stop — that is not what this
change does, and applying it would delete the corpus.

```
terraform apply
```

**Column descriptions are capped at 1,024 characters** and BigQuery rejects the
table update rather than truncating. The first apply of this change hit that on
`hand_actions`, after the other three tables had already applied — so a re-run
updates only `hand_actions`, and a plan showing one table left to change is the
expected state, not a sign something was missed. `uv run pytest` now checks the
limit, so it fails in the suite rather than at apply time.

---

## Step 1 — Mark pending, per video

Dry run first:

```
tt mark-pending --project $PROJECT --dataset $DATASET \
  --video-id <VIDEO> --stage tournament_results --dry-run
```

**Confirm before running it for real:**

- deletes `hand_setups`, `hand_starts` and `hand_actions` rows for the video;
- marks payout extraction, Phase 3, Phase 4 and Phase 5 pending;
- **leaves `clip_manifest` untouched** — it should not appear in the delete plan
  at all.

Then drop `--dry-run`.

**Do not also mark `clip_manifest`.** Re-materializing renumbers clip ids and
forces a full re-detection for no benefit: clip windows are arithmetic on
`duration_seconds` and cannot change when the payout panel is re-read.

---

## Step 2 — Payout extraction

```
tt extract-payouts --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --tournament-results-bucket $RESULTS \
  --video-id <VIDEO> --max-attempts 4
```

**Check before continuing:** the video has exactly one `tournament_results` row,
and its `bounty_type` is what you expect (`progressive` for `MPBLfM4mwfE`,
`none` for `YzKyFMQ1avU`). Phase 3 and Phase 4 both read it, and Phase 4 now
*raises* if it is missing.

```sql
SELECT video_id, bounty_type,
       JSON_VALUE(tournament_results_state, '$.provenance.models.frame') AS frame_model
FROM `table-talk-497020.table_talk_dev.tournament_results`
```

---

## Step 3 — Phase 3, hand setups

```
tt process-clips --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --hand-setups-bucket $SETUPS \
  --video-id <VIDEO> --max-attempts 4
```

Default models. **Do not set `TT_CLIP_MODEL` here** — the Pro requirement is
Phase 5's clip calls only, and the variable is shared, so exporting it would
move Phase 3's detection onto Pro at roughly 3x the token cost for no measured
benefit.

**Check:** row count is in the expected range and no clip failed. Counts moving
is expected — detection is not deterministic.

---

## Step 4 — Phase 4, hand starts

```
tt process-hand-setups --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --hand-starts-bucket $STARTS \
  --video-id <VIDEO> --max-attempts 4
```

Default models again, for the same reason.

**Check:** the stats line. `complete_skipped` now includes P4-1, P4-2 and P4-3,
which did not exist before, so a higher skip count than the last run is expected
rather than alarming. Run the per-gate report (below) to see which fired.

**P4-6 now judges the FVA seat's hole cards only.** A null on any other seat
completes here and is judged by P5-16 in step 5, so expect fewer P4-6 parks than
the run that produced the current tables — and re-mark any hand parked by the
unnarrowed gate before running this step, or it stays parked on a rule that no
longer exists. See H5 in ARCHITECTURE.

---

## Step 5 — Phase 5, hand actions

```
TT_CLIP_MODEL=gemini-2.5-pro tt process-hand-starts \
  --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --hand-actions-bucket $ACTIONS \
  --video-id <VIDEO> --max-attempts 4
```

**`TT_CLIP_MODEL` is written inline on this command, never exported.** It is
read once at import, so its scope is whichever command carries it. Exported for
the session it would silently move Phase 3's detection and Phase 4's step A onto
Pro as well.

`--max-attempts 4`, here and everywhere above: a mark is written as
`failed_transient`, so it costs one retry slot and the default 3 would leave only
two real attempts.

**Check:** P5-16 is new and permanent — a null hole card on a seat that stayed in
after the FVA. It is the first run in which it can fire at all (the gate it
splits from suppressed its whole population), so its count is the measurement,
not a regression signal. `--max-attempts` does not apply: it never retries.

---

## Step 6 — Audit

```
tt check-integrity --project $PROJECT --dataset $DATASET
```

Expect clean. It anchors on input tables, so it reports orphans, duplicate
natural ids and status/row-count mismatches — not gate outcomes.

---

## The per-gate report

Hits, recoveries and parks grouped by gate, across all three phases. The rebuild
re-runs payout extraction first, so the `PAYOUT-*` codes belong here alongside
P4 and P5.

```sql
WITH attempts AS (
  SELECT 'payout'   AS phase, video_id       AS entity_id, status, status_message, attempted_at
  FROM `table-talk-497020.table_talk_dev.tournament_results_processing_attempts`
  UNION ALL
  SELECT 'phase4',           hand_setup_id,  status, status_message, attempted_at
  FROM `table-talk-497020.table_talk_dev.hand_setup_processing_attempts`
  UNION ALL
  SELECT 'phase5',           hand_start_id,  status, status_message, attempted_at
  FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts`
),
gated AS (
  SELECT
    phase, entity_id, status, attempted_at,
    REGEXP_EXTRACT(status_message, r'(P4-\d+|P5-\d+|PAYOUT-\d+): ([a-z_]+)') AS gate_id,
    REGEXP_EXTRACT(status_message, r'(?:P4-\d+|P5-\d+|PAYOUT-\d+): ([a-z_]+)') AS code
  FROM attempts
  WHERE status_message IS NOT NULL
),
latest AS (
  SELECT entity_id,
         ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS final_status
  FROM attempts GROUP BY entity_id
)
SELECT
  g.phase,
  g.gate_id,
  g.code,
  COUNT(*)                                             AS hits,
  COUNT(DISTINCT g.entity_id)                          AS entities,
  COUNTIF(l.final_status LIKE 'complete%')             AS recovered,
  COUNTIF(l.final_status = 'failed_parked')            AS parked
FROM gated g
JOIN latest l USING (entity_id)
WHERE g.gate_id IS NOT NULL
GROUP BY g.phase, g.gate_id, g.code
ORDER BY parked DESC, hits DESC
```

`recovered` counts entities that hit a gate and finished `complete*` anyway —
the stochastic ones a retry fixed. `parked` is the population to review before
re-marking anything.

### Parked hands, listed

```sql
SELECT hand_start_id, status_message, attempted_at
FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts` a
WHERE status = 'failed_parked'
  AND attempted_at = (
    SELECT MAX(attempted_at) FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts`
    WHERE hand_start_id = a.hand_start_id)
ORDER BY status_message
```

### Boundary fragments among failures

Whether clip-boundary fragments deserve a Phase 3 precondition. One of the two
pre-rebuild P5-7(c) hits was a fragment rather than a prompt defect.

```sql
SELECT
  COUNTIF(hs.hand_setup_time_seconds = cm.clip_start_time) AS at_clip_boundary,
  COUNT(*)                                                 AS failed_or_parked
FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts` a
JOIN `table-talk-497020.table_talk_dev.hand_starts` st
  ON st.hand_start_id = a.hand_start_id
JOIN `table-talk-497020.table_talk_dev.hand_setups` hs
  ON hs.hand_setup_id = st.hand_setup_id
JOIN `table-talk-497020.table_talk_dev.clip_manifest` cm
  ON cm.clip_id = hs.clip_id
WHERE a.status IN ('failed_parked', 'failed_permanent')
```

A meaningful share is the argument for suppressing detections in the first
seconds of a clip.

---

## Verification queries

### 6 — every row carries provenance

```sql
SELECT 'tournament_results' AS t,
       COUNTIF(JSON_QUERY(tournament_results_state, '$.provenance') IS NULL) AS missing,
       COUNT(*) AS total_rows
FROM `table-talk-497020.table_talk_dev.tournament_results`
UNION ALL
SELECT 'hand_setups',
       COUNTIF(JSON_QUERY(hand_setup_state, '$.provenance') IS NULL), COUNT(*)
FROM `table-talk-497020.table_talk_dev.hand_setups`
UNION ALL
SELECT 'hand_starts',
       COUNTIF(JSON_QUERY(hand_start_state, '$.provenance') IS NULL), COUNT(*)
FROM `table-talk-497020.table_talk_dev.hand_starts`
UNION ALL
SELECT 'hand_actions',
       COUNTIF(JSON_QUERY(hand_action_state, '$.provenance') IS NULL), COUNT(*)
FROM `table-talk-497020.table_talk_dev.hand_actions`
```

`missing` must be 0 everywhere. Then confirm the hashes match the working tree:

```
git hash-object prompts/extract_player_actions.md | cut -c1-12
```

against a stored value:

```sql
SELECT DISTINCT
  JSON_VALUE(hand_action_state, '$.provenance.prompts["prompts/extract_player_actions.md"]')
FROM `table-talk-497020.table_talk_dev.hand_actions`
```

One distinct value, equal to the `git hash-object` output at the commit the run
used.

### 7 — every street carries `extraction_status`, none `unread`

```sql
SELECT
  COUNTIF(JSON_VALUE(st, '$.extraction_status') IS NULL) AS missing_status,
  COUNTIF(JSON_VALUE(st, '$.extraction_status') = 'unread') AS unread,
  COUNT(*) AS streets
FROM `table-talk-497020.table_talk_dev.hand_actions`,
UNNEST(JSON_QUERY_ARRAY(hand_action_state, '$.streets')) AS st
```

Both counts must be 0. An `unread` means something regressed.

### 8 — inert streets were skipped, and not scanned

```sql
SELECT
  JSON_VALUE(st, '$.extraction_status') AS status,
  COUNT(*) AS streets,
  COUNTIF(JSON_VALUE(st, '$.street_timestamp') IS NOT NULL) AS with_timestamp,
  COUNTIF(ARRAY_LENGTH(JSON_QUERY_ARRAY(st, '$.community_cards')) > 0) AS with_cards
FROM `table-talk-497020.table_talk_dev.hand_actions`,
UNNEST(JSON_QUERY_ARRAY(hand_action_state, '$.streets')) AS st
GROUP BY status
```

`skipped_inert` must show 0 for both `with_timestamp` and `with_cards`. Confirm
against the run log that no scan fired for them:

```
grep 'gemini_usage' run.log | grep -c 'label=step_e_scan_'
```

Compare to the `extracted` count above — they should agree, allowing for scan
retries.

### 9 — the two adjudicated hands

**Find both by timestamp, not by id** — ids renumber on re-detection.

```sql
SELECT hs.video_id, hs.hand_setup_time_seconds,
       JSON_QUERY(ha.hand_action_state, '$.streets[0].actions[0]') AS first_preflop_action
FROM `table-talk-497020.table_talk_dev.hand_actions` ha
JOIN `table-talk-497020.table_talk_dev.hand_setups` hs USING (hand_setup_id)
WHERE (hs.video_id = 'MPBLfM4mwfE' AND hs.hand_setup_time_seconds BETWEEN 2355 AND 2365)
```

Expect `SB call 1` — first time, or after a **P5-4** retry. Not P5-8: the BTN
sits above the FVA seat in preflop acting order, so it is already folded before
the sequence starts and the structural gate catches it first.

```sql
SELECT hs.hand_setup_time_seconds,
       JSON_VALUE(st, '$.extraction_status') AS status,
       JSON_VALUE(st, '$.street_timestamp')  AS ts
FROM `table-talk-497020.table_talk_dev.hand_actions` ha
JOIN `table-talk-497020.table_talk_dev.hand_setups` hs USING (hand_setup_id),
UNNEST(JSON_QUERY_ARRAY(ha.hand_action_state, '$.streets')) AS st
WHERE hs.video_id = 'YzKyFMQ1avU'
  AND JSON_VALUE(st, '$.street_name') = 'river'
  AND SAFE_CAST(JSON_VALUE(st, '$.street_timestamp') AS INT64) BETWEEN 595 AND 615
```

Expect the river `extracted` with a timestamp near 604 s.

### 10 — re-running selects nothing

Re-run any `tt process-*` with nothing pending. Expect zero processed and zero
written.

---

## After the run

Record the new counts in ARCHITECTURE's "Corpus state", which currently holds
pre-rebuild figures and says so. A change is expected — detection is not
deterministic. A large change is itself a finding.

Then the per-gate report review, which is where the standing assumption that the
gated errors are stochastic gets its first real test.
