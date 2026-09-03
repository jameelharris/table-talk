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

These conventions govern the structure and design of code in this project. They are project-specific rules that new code must follow, distinct from the general coding discipline in sections 1-4. ARCHITECTURE.md describes the phases where these conventions are applied and records the incidents that produced them; this section is the authoritative source for the rules themselves.

### Schemas are the source of truth

BigQuery table schemas live at `schemas/*.json` at the repo root. Python dataclasses are generated from them via `scripts/gen_schemas.py` and land in `src/table_talk/_generated/`. Generated files are committed and never edited by hand. After changing a schema, regenerate before committing. The same JSON also drives Terraform table creation — see "Infrastructure is Terraform-managed."

Not every value a phase needs is a column. Query-computed values (window-function results, derived counts) belong on a module-local frozen dataclass in the orchestrator, not in a schema and not hand-added to a generated row class. `PendingHandSetup` and `PendingClip` are the pattern: they carry the table's columns plus the values the pending query derives.

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

Bucket lifecycle rules must be scoped to noncurrent versions only. Frames are referenced by paths stored on stage rows and are never discovered by scanning, so an age-based rule on current versions would delete live, referenced evidence.

### Table taxonomy

Three kinds of table:

- **Fact tables** hold durable entity records. `videos` is currently the only one.
- **Stage tables** hold the output of a processing phase, consumed by later phases: `clip_manifest`, `hand_setups`, `hand_starts`, `hand_actions`. A future assembly phase combines them into fact tables.
- **State tables** are insert-only logs of processing attempts, one row per attempt, named `*_attempts`: `video_ingestion_attempts`, `clip_processing_attempts`, `hand_setup_processing_attempts`, `hand_start_processing_attempts`. They carry a server-defaulted `attempted_at`.

Each processing phase pairs a stage table with a state table. The current state of an entity is derived: the latest row in its state table by `attempted_at`. No in-flight states are stored; rows are written only after attempts conclude.

**State tables are append-only audit logs.** Never UPDATE a state row and never DELETE one. To reverse a prior outcome, insert a new row that supersedes it. A deletion destroys the record of what actually happened, which is the table's whole purpose.

**Latest status does not tell you whether output exists.** It records what the client believed at the time, which can be wrong — see "Idempotence." Several outcomes are legitimately terminal with zero stage rows, so the presence or absence of output can't be inferred from status either. Both directions of that inference are unsafe.

### Stage blobs nest their upstream verbatim

Each phase's JSON column embeds the prior phase's blob **unchanged** under a named key and adds its own contribution as a sibling. Phase 4's `hand_start_state` is `{"hand_setup": {...verbatim from hand_setups...}, "fva": {...}}`; Phase 5's `hand_action_state` is `{"hand_start": {...verbatim from hand_starts...}, "streets": [...], "winning_positions": [...]}`.

Nesting verbatim rather than flattening makes provenance obvious — any value can be traced to the phase that produced it by its position in the structure — and keeps each phase's contribution separable. The assembly phase flattens; the stage tables do not.

**Every embedded copy is a snapshot taken at write time.** Reprocessing an upstream phase does not update it, so a downstream blob can hold values its source table no longer agrees with. This is the reprocessing-cascade gap, and it compounds one layer per phase. Nothing currently detects it.

Two consequences. A phase reads its own inputs from the upstream *table*, never from a blob nested inside another row. And a manual correction belongs in the table where the value originates, not in a downstream snapshot — correcting the snapshot leaves the two records disagreeing with the upstream one still wrong.

### Attempt status semantics

Status values vary by phase, but every status falls into one of three categories, and new statuses must declare which:

- **Terminal success** — the entity is done; never re-selected. (`complete`, `complete_skipped`, `complete_uncontested`)
- **Terminal failure** — the entity cannot complete; never re-selected. (`failed_permanent`, `failed_parked`, Phase 1's `failed_terminal`)
- **Retryable** — re-selected on the next run. (`failed_transient`, Phase 1's `failed_transient_predownload` / `failed_transient_postdownload`)

`complete_skipped` is for precondition failures caught before any LLM call — the entity was examined and deliberately not processed. It is a success, not a failure: retrying would produce the same skip.

`complete_uncontested` (Phase 4) is for a correct extraction whose correct answer is that there is nothing to extract — every player folds to the BB, so there is no voluntary chip commitment to identify. Like a skip it is a success, not a failure: retrying would produce the same answer, and `failed_transient` would retry it forever.

The three terminal successes differ in whether output exists. `complete` is success *with* output; `complete_skipped` and `complete_uncontested` are successes with zero stage rows, each for a stated structural reason. That the two zero-row statuses share a `complete_*` shape is an observation about the vocabulary as it stands, not a rule — a future status should be named for what it means, and the shared shape only records that this category has more than one member.

Pending queries select entities whose latest status is retryable or absent. Adding a status means deciding its category and updating the pending query if it is retryable.

### BQ writes use DML with parameterized queries

Not load jobs. This is so BigQuery applies column DEFAULTs server-side. The codegen omits any column with `defaultValueExpression` set, since BQ supplies those values.

**Stage-table writes use replace semantics.** In a single multi-statement transaction: DELETE existing rows for the entity's natural key, then INSERT the new rows. Three properties, all required:

- **Unconditional.** The DELETE runs on every write. Nothing checks whether a row exists, and nothing compares the stage table to the state table. The path that prevents duplicates is the same path exercised on every normal write, not a rare branch that only fires during an outage.
- **At write time, after all processing.** Not a pre-flight step. A DELETE up front would leave an entity with zero rows if processing then failed.
- **Atomic.** As two separate statements, a committed DELETE followed by a failed INSERT destroys a good row — worse than the duplicate being prevented.

The DELETE must run even when there are no rows to insert, because the outcomes that most need it are the zero-row ones. This means the natural key is an explicit writer parameter, never derived from the rows, and an empty row list is not a no-op.

The natural key is the entity the phase processes, which is not always the stage row's own id: `hand_setups` keys on `clip_id`, not `hand_setup_id`, because reprocessing a clip can produce a different number of rows and per-row keying would orphan the surplus.

Replace semantics make stage writes mutating DML, which BigQuery serializes per table rather than running with the concurrency available to INSERT-only writes. This is a throughput constraint, not a correctness one: an aborted transaction surfaces as a write error, is classified retryable, and the retry is safe precisely because the write is idempotent.

`clip_manifest` is written once per video with no state table and has no replace wrapper; Phase 2's retry derives from the absence of output rows, which is the same principle applied through a different mechanism.

**`bq_param_type` covers `str`, `int` and `dict` only.** It has no `float` branch and no `None` branch, and stage writers — unlike state writers — do not filter `None` out of the row dict before building parameters. So a `FLOAT64` column or a NULLABLE scalar cannot be added to a stage table without extending `bq_utils` first: codegen and Terraform both accept them, and the failure surfaces only at the first write. This is why every `hand_actions` column is REQUIRED, and why per-row floats like `bet_amount` live inside the JSON blob, which BigQuery parses server-side and which never reaches `bq_param_type`.

Note `bool` is a subclass of `int`; a BOOL column would need its own branch checked *before* the `int` one, or `True`/`False` maps to `INT64`.

### Idempotence and self-healing

Processing functions must be idempotent on their inputs. Re-running a command must be safe and must naturally fill in missing work.

**Idempotence derives from the output table, never from the attempts table.** A state row says what the client believed happened. That belief can be wrong: a write can succeed while the client's confirmation fails, producing a failure row for completed work and returning the entity to the pending set. Any check that consults the attempts table to decide whether to write inherits the same wrong belief and permits the duplicate anyway. The output table is the only authoritative record of whether a row landed.

For the same reason, do not add an output-existence guard to a pending query. Several outcomes are legitimately terminal with zero stage rows; a `NOT EXISTS` filter would mark them permanently pending and reprocess them forever.

Replace-semantics writes are what deliver idempotence. They also keep deliberate reprocessing available — after a prompt fix, re-running an entity replaces its output rather than being skipped or duplicated.

### Retry caps and terminal parking

`failed_transient` means "retry on every run," which is wrong for an entity that deterministically cannot complete: it burns LLM calls indefinitely and never succeeds.

Orchestrators cap retries. After N consecutive failures (default 3, `--max-attempts`), write a terminal `failed_parked` instead of `failed_transient`. Parked entities stop being selected and collect in a queryable bucket for later batch diagnosis. Un-parking is a manual append of a retryable status — never an edit to the parked row.

Two rules govern the counter:

- **It counts consecutive failures since the last non-failure, not lifetime failures.** A failure can follow a success, so a lifetime count would park healthy entities early.
- **It is computed in the pending query and carried on the dataclass, never re-queried inside a failure handler.** The handler runs precisely when BigQuery may be unreachable, so a read there would fail too, and the natural fallback would silently defeat the cap for every entity an outage touches.

The current failure is not yet counted when the handler runs, so the threshold test is `consecutive_failures + 1 >= max_attempts`.

### Writers are hand-written per table

Each table has its own writer module, no shared base class. Follow the template of `videos_writer.py` for simple cases or `video_ingestion_attempts_writer.py` for cases with status validation. Some duplication across writers is accepted for consistency.

Shared *pure helpers* are fine where the duplication would be exact and mechanical — `bq_utils.bq_param_type` for parameter typing, `bq_utils.build_replace_sql` for statement assembly. The line is between a small stateless function both writers call and an abstraction that owns the write. Orchestrators are not shared at all; phases duplicate structure rather than coupling to each other.

### Batch DML for high-cardinality writes

When one operator action produces many rows for a single entity (like `clip_manifest`'s many-clips-per-video), the writer accepts a list and produces one atomic DML statement. For single-row-per-event writes (`videos`, `video_ingestion_attempts`, `clip_processing_attempts`), writers stay single-row.

The atomicity unit is the logical group that belongs together, and for stage tables it is also the replace key: a clip's hand_setups all land in one statement and are all replaced together.

### Primitives are stateless and ignorant of orchestration

Fetcher, uploader, and writers each take inputs and produce outputs (or raise classified exceptions). They do not consult BigQuery for context, do not decide retry policy, and do not know about other primitives. Orchestrators compose them.

Writers are the boundary case: a writer owns its own statement's atomicity, including the DELETE that makes it idempotent, but does not decide *whether* to write. Nothing in a primitive touches GCS on behalf of a BQ write or vice versa.

### CLI commands map to logical groups

Each `tt` subcommand does one logical group's work. Cross-group composition is via command sequence, not bundled commands. Each phase that needs operator-invoked work gets its own subcommand.

### INT64 seconds for video-offset times

All time offsets within a video (clip start/end, hand start within clip, etc.) are stored as INT64 seconds. Not floats, not milliseconds, not durations. Sub-second values may exist transiently in memory (verification frames are extracted at fractional offsets) but are never persisted.

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
DELETE FROM hand_setups WHERE ...
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

### Prompts have no automated tests

`prompts/*.md` are versioned with code so prompt changes ride code review, but nothing asserts on their content. The regression guard is reproduction: before changing a prompt, reproduce the exact failing call in a notebook against the real stored frame or video window, and confirm the fix against both the failing case and a control. Theorising about a prompt bug without looking at the actual frame has produced wrong hypotheses more than once.

One narrow exception: a test may assert a **code-to-prompt interface contract** — that a string a Python function constructs appears verbatim in the prompt file that consumes it. `call_gemini_for_clip` labels each reference image `Reference image — {label}:`, and `extract_community_cards.md` describes the images by those exact names; a test asserts the rendered labels appear in the file. Reword either side and the descriptions silently unbind from the images, degrading extraction corpus-wide with no error anywhere.

The line is between asserting a contract and asserting content. A test that checks a substitution slot exists, or that a constructed string matches what the prompt expects, is a contract test and is allowed. A test that asserts what a prompt *says* — its instructions, its examples, its wording — is not.
