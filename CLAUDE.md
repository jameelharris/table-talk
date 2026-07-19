# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.
```
---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Project Conventions

These conventions govern the structure and design of code in this project. They are project-specific rules that new code must follow, distinct from the general coding discipline in sections 1-4. ARCHITECTURE.md describes the phases where these conventions are applied; this section is the authoritative source for the rules themselves.

### Schemas are the source of truth

BigQuery table schemas live at `schemas/*.json` at the repo root. Python dataclasses are generated from them via `scripts/gen_schemas.py` and land in `src/table_talk/_generated/`. Generated files are committed and never edited by hand. After changing a schema, regenerate before committing. The same JSON also drives Terraform table creation — see "Infrastructure is Terraform-managed."

### Infrastructure is Terraform-managed

All GCP resources are provisioned via Terraform. Nothing is created by hand
(`gcloud`, `bq mk`, console) or by application code (`CREATE TABLE IF NOT EXISTS`,
bucket auto-create on first write). Three modules cover current needs:
`modules/gcs_bucket`, `modules/bigquery_dataset`, `modules/bigquery_table`.

BigQuery table modules read their schema directly from the repo's schema files
(`schema = file(".../schemas/<table>.json")`). This means `schemas/*.json` has two
consumers: `scripts/gen_schemas.py` (produces dataclasses) and Terraform (creates
the table). Both must stay in sync with the JSON.

Consequences:

- Adding a table = add `schemas/<table>.json`, run codegen, add a
  `bigquery_table` module block, apply.
- Changing a schema = regenerate *and* re-apply. Codegen alone leaves the
  deployed table stale.
- Adding a bucket = add a `gcs_bucket` module block. Bucket defaults
  (versioning, soft delete) live in the module; don't pass them per-call.
- A phase's infrastructure lands as its own focused PR, consistent with the
  commit conventions above.

### BQ writes use DML INSERT with parameterized queries

Not load jobs. This is so BigQuery applies column DEFAULTs server-side. The codegen omits any column with `defaultValueExpression` set, since BQ supplies those values.

### Two-table pattern (inventory + state machine)

Each major entity has two tables:

- An **inventory table** — immutable per row, captures "this thing exists." Written once at entity creation.
- A **state machine table** — insert-only, captures "this is what happened during an attempt to process this thing." One row per attempt. Server-defaulted `attempted_at` timestamp.

The current state of an entity is derived: latest row in the attempts table (by `attempted_at`) for that entity. No in-flight states are stored; rows are only written after attempts conclude.

Never UPDATE state machine rows; insert new rows instead.

### Writers are hand-written per table

Each table has its own writer module, no shared base class. Follow the template of `videos_writer.py` for simple cases or `video_ingestion_attemptster.py` for cases with status validation. Some duplication across writers is accepted for consistency.

### Batch DML for high-cardinality writes

When one operator action produces many rows for a single entity (like `clip_manifest`'s many-clips-per-video), the writer accepts a list and produces one atomic DML INSERT. For single-row-per-event writes (`videos`, `video_ingestion_attempts`, `clip_processing_attempts`), writers stay single-row.

The atomicity unit is the logical group that belongs together. A video's clips all land in one DML statement so partial-state idempotence failures cannot happen.

### Primitives are stateless and ignorant of orchestration

Fetcher, uploader, and writers each take inputs and produce outputs (or raise classified exceptions). They do not consult BigQuery for context, do not decide retry policy, and do not know about other primitives. Orchestrators compose them.

### CLI commands map to logical groups

Each `tt` subcommand does one logical group's work. Cross-group composition is via command sequence, not bundled commands. Each phase that needs operator-invoked work gets its own subcommand.

### INT64 seconds for video-offset times

All time offsets within a video (clip start/end, hand start within clip, etc.) are stored as INT64 seconds. Not floats, not milliseconds, not durations.

### Idempotence and self-healing

Processing functions must be idempotent on their inputs. Re-running a command should be safe and should naturally fill in any missing work. Idempotence checks derive from the attempts tables: "does any attempt with status='complete' exist for this entity?"

### Integration tests are opt-in

`uv run pytest` excludes them by default (via `addopts` in `pyproject.toml`). Run them explicitly with `uv run pytest -m integration`. Integration tests hit real GCP dev resources and must clean up in `try/finally`.

### Testing scope

Test files are scoped to a single phase. No test file imports from another phase's test files. Cross-phase composition is verified at the CLI seam by an operator running commands, not by automated tests that span multiple phases.

Within each phase, every production file has a corresponding test file containing both unit tests (most) and integration tests (some, marked `@pytest.mark.integration`). The phase's overall correctness emerges from the union of integration tests across its files, not from any single phase-level test.

**Cross-phase setup via production writers.** When a phase's integration tests require state from earlier phases (e.g., Phase 3 tests need a `videos` row and a `clip_manifest` row to exist), they call earlier phases' production writers as setup utilities — not earlier phases' orchestrators.

For example, a Phase 3 integration test would:

```
# Setup via production writers from Phase 1 and Phase 2
write_video_row(VideosRow(...))
write_clip_manifest_rows([ClipManifestRow(...)])

# Exercise Phase 3
phase3_function(clip_id, ...)

# Assert Phase 3 outputs
...

# Cleanup in reverse dependency order
DELETE FROM hand_setups WHER...
DELETE FROM clip_manifest WHERE ...
DELETE FROM videos WHERE ...
```

Writers are stateless functions with well-defined contracts and their own tests. Reusing them as setup utilities is clean. Reusing orchestrators (`process_url`, `materialize_clips_for_pending_videos`) would couple the test to too much behavior and would slow tests down with unnecessary work (e.g., real YouTube downloads).

### Integration test scoping

Integration tests must operate only on data they create. This has two implications:

**For tests:** an integration test must not invoke a function whose scope exceeds the test's owned data. Tests insert test rows (using uuid-based IDs), call functions limited to those IDs, assert outcomes, and clean up only those IDs.

**For functions:** any function that scans for "all X matching some condition" must accept a scope-limiting parameter (e.g. `only_X_ids: list[str] | None = None`) so that integration tests can constrain the function's blast radius. Production callers leave the parameter as default (`None`) to operate on the full set; tests provide the IDs they own.

Functions that take a specific identifier as an argument (a video_id, a row, a URL) naturally scope to that identifier and don't need additional scope-limiting parameters.

The principle is enforced by code review — there is no automated check.
