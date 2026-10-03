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

**Four more descriptions changed since this runbook was written** — the JSON
state column on `tournament_results`, `hand_setups`, `hand_starts` and
`hand_actions` all now document `provenance.media_resolution`. Same shape as
before: descriptions only, no column, type or mode changes, and codegen output
byte-identical. Apply before Step 1.

**The five `*_attempts` tables' `status` descriptions changed too**, for
`marked_pending` — and `clip_processing_attempts` gained the `failed_parked` its
description had always omitted. Descriptions only; codegen output is unchanged.
Apply before Step 1.

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
  --video-id <VIDEO>
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
  --video-id <VIDEO>
```

Default models, and nothing to set. Phase 3's detection runs on
`TT_HAND_SETUP_CLIP_MODEL`, which defaults to Flash in code and is the only
phase that variable touches — the shared `TT_CLIP_MODEL` that used to make this
a warning is removed, and setting it now fails the command outright.

**Check:** row count is in the expected range and no clip failed. Counts moving
is expected — detection is not deterministic.

---

## Step 4 — Phase 4, hand starts

```
tt process-hand-setups --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --hand-starts-bucket $STARTS \
  --video-id <VIDEO>
```

Default models again, for the same reason.

**The hole-card read now runs at `MEDIA_RESOLUTION_ULTRA_HIGH`**, which is the
measured fix for spade-face-cards-read-as-hearts. It costs roughly +1,200 input
tokens per frame read and changes no prompt, so `extract_hole_cards.md`'s hash is
unchanged from the previous run. The provenance block now carries the resolution
alongside the model, so rows from this run are still distinguishable from earlier
ones:

```sql
SELECT JSON_VALUE(hand_start_state, '$.provenance.media_resolution.frame') AS res,
       COUNT(*)
FROM `table-talk-497020.table_talk_dev.hand_starts`
GROUP BY res
```

Expect `MEDIA_RESOLUTION_ULTRA_HIGH` for every row after this run. Rows written
before the field existed have no `media_resolution` key at all, which reads as
NULL and is the correct answer for them. See ARCHITECTURE, "Suit misreads are a
resolution problem."

**Check:** the stats line. `complete_skipped` now includes P4-1, P4-2 and P4-3,
which did not exist before, so a higher skip count than the last run is expected
rather than alarming. Run the per-gate report (below) to see which fired.

**P4-7 is new and `failed_transient`** — an FVA whose amount cannot be the seat's
whole stack plus its posted blind. A hit is a retry, not a skip, so it shows up in
`failed_transient` rather than `complete_skipped`, and it parks at
`--max-attempts` like any other. It is the gate that would have caught
`YzKyFMQ1avU_014_003` here instead of as a P5-8 in step 5, where every retry cost
a Pro call and failed identically. All 143 stored `hand_starts` FVA blocks pass
it, so a hit on this run is news.

**P4-6 now judges the FVA seat's hole cards only.** A null on any other seat
completes here and is judged by P5-16 in step 5, so expect fewer P4-6 parks than
the run that produced the current tables — and re-mark any hand parked by the
unnarrowed gate before running this step, or it stays parked on a rule that no
longer exists. See H5 in ARCHITECTURE.

---

## Step 5 — Phase 5, hand actions

```
tt process-hand-starts \
  --project $PROJECT --dataset $DATASET \
  --videos-bucket $VIDEOS --hand-actions-bucket $ACTIONS \
  --video-id <VIDEO>
```

Step E's community-card read also runs at `MEDIA_RESOLUTION_ULTRA_HIGH` now. No
board misread was ever reproduced, so this one is prophylactic.

**No model variable on this command any more.** Phase 5's clip calls default
to `gemini-3.1-pro-preview` in code, on `TT_HAND_ACTION_CLIP_MODEL`, which no
other phase reads. The old `TT_CLIP_MODEL=gemini-2.5-pro` prefix is gone twice
over: 2.5 Pro retires on Agent Platform by 16–20 October, and the variable it
was set through is removed. If you have it exported from an earlier session,
every `tt` command will now fail with a message naming the per-phase
replacements — unset it. See ARCHITECTURE, "Phase 5's clip model is
`gemini-3.1-pro-preview`."

The default `--max-attempts 3` is right everywhere above: a mark is written as
`marked_pending`, which resets the consecutive-failure count, so every marked
entity starts the run with all three attempts. Raising it is no longer needed.

**A step-D gate failure identical to the previous real attempt's now ends the hand
`failed_permanent`**, so the attempt budget no longer implies that many step-D
calls on Pro for an error that lives upstream — the second identical hit is the
last one.
Marks are not counted as real attempts for this, so re-marking between runs does
not reset it. Read such a park as "review Phase 3 or Phase 4 for this hand," not
as a Phase 5 defect: `tt mark-pending --stage hand_starts` after fixing the
upstream value is the way back. Gate failures only — a repeated 429 or a repeated
card-read failure still retries.

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

Hits, recoveries, parks and permanent failures across all three phases, grouped
by gate where there is one and **by failure code where there is not**. The rebuild
re-runs payout extraction first, so the `PAYOUT-*` codes belong here alongside
P4 and P5.

**Not every terminal failure carries a gate id, and the gate-only version of this
query hid the ones that matter most.** Phase 4's step A reports
`failed_parked: no_first_voluntary_commitment_found` — the orchestrator's own
`found: false` reason, with no `P4-<n>:` prefix — and all three of the rebuild's
parked hands landed there. A report keyed on the gate regex alone showed zero
parks and read as a clean run. So the bucket key falls back: a gate id when the
message has one, the bare code the orchestrator wrote after the status prefix
when it does not, and the first 40 characters otherwise so that infrastructure
errors (429s, 5xx, the token-limit 400) collapse into a few rows rather than one
per attempt.

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
-- Every failure, gated or not. Marks are excluded by both tests: `marked_pending`
-- is outside the `failed%` family, and the message filter catches the historical
-- marks written as `failed_transient` before that status existed.
classified AS (
  SELECT
    phase, entity_id, attempted_at,
    COALESCE(
      REGEXP_EXTRACT(status_message, r'(P4-\d+|P5-\d+|PAYOUT-\d+)'),
      '(no gate id)'
    ) AS gate_id,
    COALESCE(
      REGEXP_EXTRACT(status_message, r'(?:P4-\d+|P5-\d+|PAYOUT-\d+): ([a-z0-9_]+)'),
      REGEXP_EXTRACT(status_message, r'^failed_[a-z_]+: ([a-z0-9_]+)$'),
      SUBSTR(REGEXP_REPLACE(status_message, r'^failed_[a-z_]+: ', ''), 1, 40)
    ) AS code
  FROM attempts
  WHERE status LIKE 'failed%'
    AND status_message IS NOT NULL
    AND status_message NOT LIKE 'mark-pending: rebuilding %'
),
latest AS (
  SELECT entity_id,
         ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS final_status
  FROM attempts GROUP BY entity_id
)
SELECT
  c.phase,
  c.gate_id,
  c.code,
  COUNT(*)                                                                   AS hits,
  COUNT(DISTINCT c.entity_id)                                                AS entities,
  COUNT(DISTINCT IF(l.final_status LIKE 'complete%',       c.entity_id, NULL)) AS recovered,
  COUNT(DISTINCT IF(l.final_status = 'failed_parked',      c.entity_id, NULL)) AS parked,
  COUNT(DISTINCT IF(l.final_status = 'failed_permanent',   c.entity_id, NULL)) AS permanent
FROM classified c
JOIN latest l USING (entity_id)
GROUP BY c.phase, c.gate_id, c.code
ORDER BY parked DESC, permanent DESC, hits DESC
```

`recovered` counts entities that failed and finished `complete*` anyway — the
stochastic ones a retry fixed. `parked` and `permanent` are the population to
review before re-marking anything. All three counts are distinct entities, not
rows, so an entity that hit the same code twice is counted once.

Add `AND attempted_at >= '<run start>'` to `classified` to scope the report to one
rebuild; on the current corpus every gate hit belongs to the rebuild, so the
scoped and unscoped reports agree. Unscoped it also lists manual un-park rows
(`unparked: retry after ...`), which are retryable statuses written by hand rather
than failures — another reason to scope it to the run under review.

### Parked hands, listed

The join to `hand_starts` is not decoration. The attempts table still holds the
history of hands re-detection has removed, so without it this lists ids that no
longer exist.

```sql
SELECT a.hand_start_id, a.status_message, a.attempted_at
FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts` a
JOIN `table-talk-497020.table_talk_dev.hand_starts` st
  ON st.hand_start_id = a.hand_start_id
WHERE a.status = 'failed_parked'
  AND a.attempted_at = (
    SELECT MAX(attempted_at) FROM `table-talk-497020.table_talk_dev.hand_start_processing_attempts`
    WHERE hand_start_id = a.hand_start_id)
ORDER BY a.status_message
```

Run the same query against `hand_setup_processing_attempts` joined to
`hand_setups` for Phase 4's parks — which is where the rebuild's three were.

### Duplicate detections among failures

Whether near-duplicate detections deserve a Phase 3 precondition. The test is the
**nearest neighbouring detection**, not the failing row's own position.

**The earlier version of this query tested `hand_setup_time_seconds =
clip_start_time` on the failing row and returned 0, which was wrong in a way that
inverted its conclusion.** In a boundary pair the row that fails is the *earlier*
one — the hand whose Phase 4 LEAD window the re-detection collapsed to a few
seconds — and that row sits just *before* the boundary, never on it. The row at
`clip_start_time` is the one that completes. The query has to look at the pair.

```sql
WITH setups AS (
  SELECT
    hs.hand_setup_id, hs.clip_id,
    hs.hand_setup_time_seconds               AS t,
    LAG(hs.hand_setup_time_seconds)  OVER w  AS prev_t,
    LAG(hs.clip_id)                  OVER w  AS prev_clip,
    LEAD(hs.hand_setup_time_seconds) OVER w  AS next_t,
    LEAD(hs.clip_id)                 OVER w  AS next_clip
  FROM `table-talk-497020.table_talk_dev.hand_setups` hs
  WINDOW w AS (PARTITION BY hs.video_id ORDER BY hs.hand_setup_time_seconds)
),
neighboured AS (
  SELECT
    hand_setup_id, t,
    LEAST(IFNULL(t - prev_t, 1000000), IFNULL(next_t - t, 1000000)) AS nearest_gap,
    IF(IFNULL(t - prev_t, 1000000) <= IFNULL(next_t - t, 1000000),
       prev_clip != clip_id,
       next_clip != clip_id)                                       AS neighbour_in_another_clip
  FROM setups
),
terminal AS (
  SELECT hand_setup_id FROM (
    SELECT hand_setup_id,
           ARRAY_AGG(status ORDER BY attempted_at DESC LIMIT 1)[OFFSET(0)] AS final_status
    FROM `table-talk-497020.table_talk_dev.hand_setup_processing_attempts`
    WHERE hand_setup_id IN (
      SELECT hand_setup_id FROM `table-talk-497020.table_talk_dev.hand_setups`)
    GROUP BY hand_setup_id)
  WHERE final_status IN ('failed_parked', 'failed_permanent')
)
SELECT
  COUNT(*)                                                                  AS terminally_failed_hands,
  COUNTIF(n.nearest_gap <= 8)                                               AS has_neighbour_within_8s,
  COUNTIF(n.nearest_gap <= 8 AND n.neighbour_in_another_clip)                AS boundary_fragments,
  COUNTIF(n.nearest_gap <= 8 AND NOT n.neighbour_in_another_clip)            AS within_clip_duplicates,
  STRING_AGG(IF(n.nearest_gap <= 8,
                FORMAT('%s@%d gap=%d', n.hand_setup_id, n.t, n.nearest_gap),
                NULL), '; ')                                                AS pairs
FROM terminal t
JOIN neighboured n USING (hand_setup_id)
```

The denominator is hands whose *latest* status is terminal failure, not attempts,
so a hand that failed once and later completed does not inflate it.

On the rebuilt corpus this returns **2 of 4** terminally failed hands with a
neighbour within 8 s — one boundary fragment (`MPBLfM4mwfE_008_004`, gap 6 s) and
one within-clip duplicate (`YzKyFMQ1avU_017_002`, gap 1 s). Half the terminal
failures are duplicate-pair members, which is the argument for a precondition —
and because one of the two is 145 s inside its clip, a rule that only suppresses
detections in the first seconds of a clip would catch just one of them. Proximity
in time, not proximity to a boundary, is the usable signal.

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
  JSON_VALUE(hand_action_state, '$.provenance.prompts."prompts/extract_player_actions.md"')
FROM `table-talk-497020.table_talk_dev.hand_actions`
```

One distinct value, equal to the `git hash-object` output at the commit the run
used.

**The key is a double-quoted member, not a bracket.** Provenance keys carry
slashes and a dot, and BigQuery's JSONPath has no `["..."]` form — writing it that
way fails with `400 Invalid JSON Path`. This query carried the bracket form until
it was first run.

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
       JSON_VALUE(st, '$.extraction_status')          AS status,
       JSON_VALUE(st, '$.street_timestamp')           AS ts,
       TO_JSON_STRING(JSON_QUERY(st, '$.community_cards')) AS cards
FROM `table-talk-497020.table_talk_dev.hand_actions` ha
JOIN `table-talk-497020.table_talk_dev.hand_setups` hs USING (hand_setup_id),
UNNEST(JSON_QUERY_ARRAY(ha.hand_action_state, '$.streets')) AS st
WHERE hs.video_id = 'YzKyFMQ1avU'
  AND hs.hand_setup_time_seconds BETWEEN 486 AND 490
  AND JSON_VALUE(st, '$.street_name') = 'river'
```

Expect the river `extracted` as **`["4d"]` at 580 s**. Anchor on the hand's start
(t≈488 s), not on the river's timestamp — a run that finds no river returns no
row either way, and a filter on the river timestamp cannot tell the two apart.

**The earlier expectation of "near 604 s" was wrong.** 604 s is where the
investigation under "Model selection is per call mode" said the river becomes
visible; both the pre-rebuild and the rebuilt run read it at 580 s, with
identical flop (504 s) and turn (554 s). The 580 s reading is the one to expect.

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
