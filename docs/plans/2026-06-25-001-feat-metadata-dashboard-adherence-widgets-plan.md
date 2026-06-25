---
title: "feat: Metadata dashboard Tier 2 — adherence heatmap, sparklines, enrollment"
type: feat
status: completed
date: 2026-06-25
deepened: 2026-06-25
origin: docs/plans/2026-06-24-001-feat-metadata-index-dashboard-plan.md  # Tier 2 builds on the dashboard
---

# feat: Metadata dashboard Tier 2 — adherence heatmap, sparklines, enrollment

## Summary

Add per-participant **daily** aggregates to the Upload Metadata Index — a sharded participant×day
rollup and a day-granular first-seen pointer, written additively by the collection-layer Lambda — and
three server-rendered widgets on the per-study dashboard: a **participant×day adherence heatmap**,
**per-participant volume sparklines**, and an **enrollment / first-seen-over-time** curve. Existing
deployments are populated **without wiping DynamoDB**, via a one-time backfill that derives the new
items from the per-(participant,stream) rollups already in the table during a brief ingestion pause
(disable the rule, **wait** for the queue to drain into the rollups, then SET).

---

## Problem Frame

The dashboard (see origin: `docs/plans/2026-06-24-001-feat-metadata-index-dashboard-plan.md`) shows
study- and stream-level activity but nothing at **participant-day granularity** — yet "is each
participant uploading day to day?" (adherence) and "when did participants come online?" (enrollment)
are core research-monitoring questions. The data needed isn't cheaply readable today: per-(participant,
stream) daily rollups exist but are keyed per participant+stream (`STUDY#<study>#P#<patient>#S#<stream>`),
so assembling a study-wide participant×day matrix from them is the unbounded per-participant fan-out the
design exists to avoid. This plan adds a pre-aggregated, bounded-read source for those views.

---

## Requirements

- R1. Participant×day **adherence heatmap** per study (cells = upload count per participant per day), as a bounded read independent of participant count.
- R2. **Per-participant volume sparklines** (each participant's daily series) in the freshness view.
- R3. **Enrollment / first-seen-over-time** view (cumulative participants by first-upload **day**).
- R4. New aggregates are written **additively inside the existing dedupe-gate + compensation saga** (no under-count / split-brain), and **sharded** so no single partition absorbs every upload.
- R5. Reads stay **bounded** (`Query`/`begins_with` only, never `Scan`) and **server-rendered** (inline SVG / CSS grid, no new JS dependency); failures degrade **loudly**, never as silently-wrong data.
- R6. Existing deployments are populated **without data loss** via a non-destructive backfill: disable ingestion → **wait for the queue+DLQ to drain** (never purge a live table) → SET-from-existing → resume. Wipe+redeploy reset remains a documented alternative.
- R7. The new items are **participant-scoped**, so the participant-erasure runbook and access-pattern docs must cover them, with prefix-collision-safe deletes.
- R8. The shard function is **deterministic and identical** across writer/reader/backfill (never Python's randomized `hash()`); `SHARDS` is **persisted in the table and asserted by the reader** so a mismatch fails loud instead of dropping participants.

---

## Scope Boundaries

- Not adding interactive / JS charting (uPlot etc.) — server-rendered only; uPlot remains a deferred option if interactivity is later wanted.
- Not building Tier-3 widgets (expected-vs-actual stream coverage, device-time-vs-upload-time lag, time-of-day patterns).
- Not changing the existing rollups, the existing widgets, the upload path, Django models, or the Celery pipeline.
- Not storing sub-day first-seen precision (the per-stream rollups carry no timestamp; first-seen is day-granular by construction).

### Deferred to Follow-Up Work

- Heatmap participant pagination / virtualization for very large studies — windowed by days for now; pagination is a later iteration (shared concern with the Tier-1 freshness table). If a study exceeds a row cap, the heatmap shows the most-recently-active N and a "+M more" note rather than rendering thousands of rows.
- An automated participant-erasure helper script spanning all item types/shards (the origin plan already deferred a general erasure helper).

---

## Context & Research

### Relevant Code and Patterns

- `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py` — `_claim_and_count` applies atomic `ADD`s within the per-object dedupe gate and **compensates on failure**; both the apply loop and the compensation loop currently rebuild the SK as `DAY#<day>`. `_advance_latest_pointer` is the advance-only conditional-write pattern to mirror (inverted) for first-seen. All existing rollups share SK `DAY#<day>`; the new participant-daily item uses `P#<patient>#DAY#<day>`, so the rollup target list and the `applied`/compensation bookkeeping must become `(pk, sk)` tuples.
- `libs/metadata_index_reader.py` — `study_summary` runs per-study `Query`s; `aggregate_daily`/`aggregate_latest`/`_build_views`/`_query` (paginated, Query-only); module docstring mandates **no logging of Query responses / PII**; refreshable STS creds; tests patch `_query`/`_get_table`.
- `cluster_management/cdk/show_metadata_index.py` + its test `lambdas/metadata_index/tests/test_show_metadata_index.py` (inserts `parents[2]` to import from the cdk dir) — the pattern for the backfill script + its test location, and for a boto3 cdk-tree script.
- `frontend/templates/metadata_dashboard/metadata_dashboard.html` — server-rendered tables + CSS `progress-bar` chart; Bootstrap-3; autoescaped `patient_id`; three-state degradation (not-configured / read-error / empty). No existing inline-SVG widget — the new SVG widgets establish that pattern.
- `cluster_management/cdk/deploy_metadata_index.sh` + `Makefile` `metadata-index-*` targets — subcommand + preview/`--apply` pattern; the `reset` flow's **purge** is correct only because it wipes — the backfill must NOT reuse purge (see Key Technical Decisions).
- `cluster_management/cdk/lambdas/metadata_index/tests/test_handler.py` — moto saga tests incl. the **call-count fault-injection** tests that monkeypatch `_add_rollup` by call number.
- `cluster_management/cdk/METADATA_INDEX.md` — access-pattern table, erasure runbook (uses `begins_with` on participant prefixes — shares the prefix-collision caveat), reset/backfill runbooks.

### Institutional Learnings

- None — `docs/solutions/` is empty; no `AGENTS.md`.

### External References

- Skipped — strong local patterns; the prior two plans plus this session establish all conventions.

---

## Key Technical Decisions

- **Sharded participant-daily aggregate** `STUDY#<study>#DAILY#<shard>` / SK `P#<patient>#DAY#<YYYY-MM-DD>` (`count`/`bytes` via atomic `ADD`), `SHARDS = 8` (proposed). Sharding spreads the per-upload write across 8 partitions (the single-hot-partition exposure the origin plan flagged for `STUDY#<study>`); the reader does 8 bounded `begins_with("P#")` Queries and merges — still independent of participant count. Always written (no kill-switch; sharding chosen instead, user decision).
- **Deterministic shard function** `_shard_for(patient) = zlib.crc32(patient.encode()) % SHARDS` — never `hash()`. Identical in the Lambda (vendored), `libs/` (vendored copy), and the backfill (imports the Lambda's via a `sys.path` insert, like `test_show_metadata_index.py`). **`SHARDS` is persisted** as a `CONFIG` / `SHARDS` item written by the writer/backfill; the reader `GetItem`s it once and **asserts** it equals its compiled-in constant, raising the existing loud read-error on mismatch. Rationale: a silent reader/writer `SHARDS` drift would drop whole shards of participants — indistinguishable from non-adherence; the persisted-assert converts that into a loud failure (1 GetItem, fits the reader role). This replaces the previously-deferred drift guard.
- **Day-granular first-seen pointer** `STUDY#<study>` / `FIRST#P#<patient>` carrying **`first_day`** only (`YYYY-MM-DD`), written **advance-earliest** (condition `attribute_not_exists(first_day) OR first_day > :d`; ISO-date string compare == chronological). Rationale: the per-stream `DAY#` rollups store no timestamp, so the backfill can derive only the minimum day — making `first_day` the single source both the live writer and backfill can agree on (no string-vs-timestamp comparison hazard). `first_upload_time`/`first_key` are dropped (not derivable; not needed by the enrollment view).
- **Generalize the saga to `(pk, sk)` targets.** The participant-daily item's SK differs from `DAY#<day>`, so the rollup target list, the `applied` list, **and the compensation loop** all carry `(pk, sk)` tuples (not PKs). The participant-scoped per-(patient,stream) rollup stays **first** in the list; the participant-daily target is appended **last** (after the `WRITE_STUDY_ROLLUP`-gated study total) so the existing call-#1/#2 fault tests keep their targets, plus a new test faults the participant-daily ADD specifically.
- **Non-destructive backfill = disable-rule → drain-by-wait → SET → resume.** The new items are derived from the authoritative per-(participant,stream) `DAY#` rollups via **SET (absolute)**. The pause must let the live writer finish: disable the EventBridge rule, then **wait until the queue and DLQ reach zero** (in-flight included) so all already-delivered uploads land in the existing rollups — **do not purge** (purging a live table discards uploads from every rollup, the central correctness bug the reset's purge would introduce here). The backfill **asserts the rule is disabled and the queues are empty before any SET** and aborts otherwise. Re-runnable **only while paused** (SET races live ADD if ingestion is active). Wipe+redeploy reset stays the documented "clean slate" alternative.
- **Backfill enumerates the per-stream rollup items directly** (admin-side `Scan` filtered to `STUDY#…#P#…#S#` / `DAY#`, or an explicit `--study` list — prefer the list to avoid Scan), **not** via `LATEST#` pointers — so a pair with rollup data but a missing/erased `LATEST#` pointer is not silently skipped. Aggregate per (study, patient, day) → SET participant-daily; MIN day per (study, patient) → SET `FIRST#`. Admin creds with a **minimal policy** (`dynamodb:Query`/`UpdateItem` on the table ARN; `Scan` only if the Scan-enumeration path is chosen), separate from the reader role.
- **Heatmap encodes upload count** (the adherence signal: did they upload, how much) as the primary cell shade, with bytes available secondarily; window = trailing **60 days from today** (stated, so a study with an old activity gap reads correctly). Cells distinguish four states explicitly (see U3).
- **No reader-role IAM change** — new partitions are on the same table the role already covers.

---

## Open Questions

### Resolved During Planning

- Wipe DynamoDB again? — **No.** Non-destructive backfill (disable → drain-by-wait → SET → resume).
- Single hot partition vs shard? — **Shard by patient** (`SHARDS=8`), user-confirmed; `SHARDS` persisted + reader-asserted.
- First-seen precision? — **Day-granular** (`first_day`); sub-day not derivable and not needed.
- Backfill enumeration source? — The **per-stream rollup items directly** (admin Scan or `--study` list), not `LATEST#` pointers.
- Reader IAM? — No change (same table).

### Deferred to Implementation

- Final `SHARDS` value and `crc32` vs `sha1`-mod — any deterministic, identical-everywhere choice.
- Heatmap cell color scale (linear vs quantile) and exact SVG dimensions/axis labels — tune against a rendered page.
- Whether the backfill defaults to `--study` list vs `Scan` — decide against the table's production size (Scan needs the extra IAM action and lengthens the pause).
- Row cap / "+M more" threshold for very large studies — pick against the real participant count.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

New DynamoDB items (additive):

| Item | PK | SK | Attrs | Written |
|---|---|---|---|---|
| Participant daily aggregate (sharded) | `STUDY#<study>#DAILY#<shard>` | `P#<patient>#DAY#<YYYY-MM-DD>` | `count`, `bytes` (ADD) | every upload, in the saga |
| First-seen pointer (day-granular) | `STUDY#<study>` | `FIRST#P#<patient>` | `first_day` | advance-earliest, every delivery |
| Shard-count config | `CONFIG` | `SHARDS` | `value` | writer/backfill; reader asserts |

Read access patterns added (per study, bounded):

| View (R) | PK | SK condition | Cost |
|---|---|---|---|
| Adherence matrix + per-participant sparklines (R1, R2) | `STUDY#<study>#DAILY#<shard>` for each shard | `begins_with(SK, "P#")` | `SHARDS` Queries, merged |
| Enrollment / first-seen (R3) | `STUDY#<study>` | `begins_with(SK, "FIRST#P#")` | 1 Query |
| Shard-count assert (R8) | `CONFIG` | `SHARDS` | 1 GetItem |

Page-cost delta ≈ `SHARDS + 2` (bounded; independent of participant count). Rollout:

```mermaid
flowchart LR
  A[deploy writer\n(sharded rollup + FIRST# + CONFIG/SHARDS)] --> B[disable EventBridge rule]
  B --> C[WAIT: queue + DLQ drain to 0\n(live writer finishes into existing rollups)]
  C --> D[assert rule disabled + queues empty]
  D --> E[backfill: read per-stream rollups ->\nSET participant-daily + FIRST# (absolute)]
  E --> F[re-enable rule]
  F --> G[reader asserts SHARDS, renders adherence/sparklines/enrollment]
```

---

## Implementation Units

- U1. **Writer: sharded participant-daily aggregate + day-granular first-seen + persisted SHARDS**

**Goal:** The Lambda writes the sharded `STUDY#<study>#DAILY#<shard>` / `P#<patient>#DAY#` aggregate inside the dedupe/compensation saga (generalized to `(pk, sk)` targets), an advance-earliest `FIRST#P#<patient>` day pointer, and a `CONFIG`/`SHARDS` item; via a deterministic shard function.

**Requirements:** R4, R8.

**Dependencies:** None.

**Files:**
- Modify: `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py` (`SHARDS` const + `_shard_for`; `_participant_daily_pk`; change the rollup target list, the `applied` list, **and the compensation loop** to `(pk, sk)` tuples; append the participant-daily target last; `_advance_first_pointer` (day-granular, advance-earliest); ensure-write the `CONFIG`/`SHARDS` item; no logging of PK/SK/PII)
- Modify: `cluster_management/cdk/lambdas/metadata_index/tests/test_handler.py` (assert the sharded item, day-FIRST#, CONFIG; **update call-count fault tests** + add one that faults the participant-daily ADD)

**Approach:**
- `_shard_for(patient) = zlib.crc32(patient.encode()) % SHARDS` (deterministic). Participant-daily target = `(_participant_daily_pk(study, patient), f"P#{patient}#DAY#{day}")`, appended last.
- Generalize `_add_rollup` (or its callers) so apply and compensation both operate on `(pk, sk)` tuples; the `applied` list holds tuples so a failed participant-daily ADD compensates the correct SK.
- `_advance_first_pointer`: SET `first_day` with condition `attribute_not_exists(first_day) OR first_day > :d`; call in `write_record` alongside the latest-pointer advances (idempotent, outside the count saga).
- Ensure the `CONFIG`/`SHARDS` item exists (cheap conditional/idempotent put) so the reader can assert against it.

**Patterns to follow:** `_add_rollup`/`_claim_and_count` (saga + compensation), `_advance_latest_pointer` (invert to keep-minimum), `_study_stream_rollup_pk`; the no-PII-logging rule from `metadata_index_reader.py`'s docstring.

**Test scenarios:**
- Happy path: N uploads for one (patient, day) → `STUDY#<study>#DAILY#<shard(patient)>` / `P#<patient>#DAY#<day>` has `count==N`, `bytes==sum`, in the expected shard.
- Two patients hashing to different shards → distinct partitions, each correct.
- First-seen: first delivery sets `first_day`; a later-day upload doesn't change it; an **earlier**-day upload arriving later lowers it.
- Idempotency: replaying the same object (within TTL) doesn't double-count the participant-daily aggregate.
- Compensation: a simulated ADD failure compensates all applied `(pk, sk)` targets (including participant-daily, with its `P#…#DAY#` SK, not `DAY#`) and deletes the marker; retry recounts once. Existing call-#1/#2 tests retarget correctly (per-stream first); a new test faults the participant-daily ADD (last) and asserts its compensation.
- CONFIG: the `CONFIG`/`SHARDS` item is present with the writer's `SHARDS`.
- Determinism: `_shard_for("abc")` stable across processes (not `hash()`).

**Verification:** `pytest cluster_management/cdk/lambdas/metadata_index/tests/` green; smoke upload lands in the right shard + sets `first_day` + CONFIG.

---

- U2. **Reader: query sharded matrix + first-seen, assert SHARDS, build views**

**Goal:** Extend `study_summary` to assert the persisted `SHARDS`, read the shard partitions and `FIRST#` pointers, and return the participant×day matrix, per-participant daily series, and enrollment series — bounded, loud on shard mismatch, no PII in logs.

**Requirements:** R1, R2, R3, R5, R8.

**Dependencies:** U1.

**Files:**
- Modify: `libs/metadata_index_reader.py` (`SHARDS` + vendored `_shard_for`; `GetItem` the `CONFIG`/`SHARDS` item and raise the read-error signal if it != the compiled constant; query each shard `begins_with("P#")` and merge; query `begins_with("FIRST#P#")`; build `adherence` (windowed matrix + per-participant series + totals) and `enrollment` (cumulative by `first_day`); add to `study_summary`; log by reason only)
- Test: `tests/test_metadata_index_reader.py`

**Approach:**
- Assert `SHARDS` first; on mismatch raise the typed read-error (loud) rather than serving partial data.
- Merge `SHARDS` query results into `{patient: {day: {count, bytes}}}`; derive sparkline series + totals; window the matrix to the last ~60 days. Enrollment = participants grouped by `first_day`, cumulative ascending. Keep `_query` (Query-only, paginated).

**Execution note:** Build the pure merge/aggregation against canned responses test-first; no live AWS.

**Patterns to follow:** `aggregate_daily`/`aggregate_latest`/`_build_views`/`_query`; the loud read-error mapping already in `study_summary`.

**Test scenarios:**
- Happy path: canned items across 2 shards → merged matrix has both patients with correct per-day counts; series + totals correct.
- Shard merge: a patient in shard 3 still appears (all shards queried).
- SHARDS assert: CONFIG value ≠ compiled constant → raises the read-error signal; equal → proceeds.
- Enrollment: first_days {d1, d1, d3} → cumulative [d1→2, d3→3], ascending.
- Window: days older than the window excluded from the matrix; enrollment still reflects true `first_day`s.
- Empty: no adherence/first items → empty matrix/enrollment, no exception.
- Bounded reads: exactly `SHARDS` adherence Queries + 1 first-seen Query + 1 CONFIG GetItem; `scan` never called.
- No PII: an injected error logs by reason only (no PK/patient).

**Verification:** `pytest tests/test_metadata_index_reader.py` green (host-verifiable, dummy env); grep shows no `scan(`; mismatch path raises loud.

---

- U3. **Template widgets: adherence heatmap, sparklines, enrollment curve**

**Goal:** Render the three widgets server-side (inline SVG / CSS grid, no JS lib) with explicit cell-state semantics, accessibility fallback, and a pre-backfill state.

**Requirements:** R1, R2, R3, R5.

**Dependencies:** U2.

**Files:**
- Modify: `frontend/templates/metadata_dashboard/metadata_dashboard.html`
- Test: `tests/test_metadata_dashboard_endpoints.py`

**Approach:**
- **Heatmap** rows=participants, cols=days (trailing 60). **Four distinct cell renderings:** (a) before the participant's `first_day` (pre-enrollment) — blank/neutral; (b) enrolled but zero uploads that day — distinct "gap" shade; (c) has uploads — count-shaded; (d) outside the window — not rendered. Color encodes **count**; include a legend and a non-color fallback (`title`/`aria-label` per cell with patient/day/count). Empty state when no adherence data.
- **Pre-backfill state:** when `has_data` is true but the adherence matrix is empty (writer deployed, backfill not yet run), show a "run the backfill to populate" note rather than a blank section that looks like non-adherence.
- **Sparklines:** inline-SVG polyline per participant placed in the freshness panel heading row (next to last-upload), from the daily series.
- **Enrollment:** inline-SVG cumulative line at study level with dated X / count Y ticks.
- Escape `patient_id` (autoescape). Endpoint unchanged — new views flow through `summary`.

**Patterns to follow:** existing widgets/states/escaping in the template; `show_metadata_index.py:bar()` scaling idea.

**Test scenarios:**
- Happy path: canned adherence+enrollment → heatmap (a known patient/day cell present), ≥1 sparkline, enrollment section (`assert_present`).
- Cell states: a pre-enrollment cell and an enrolled-zero cell render differently (distinct class/marker present).
- Pre-backfill: `has_data` true + empty matrix → the "run the backfill" note, page 200, no template error.
- Empty: no data → empty states, page 200.
- Security: a `patient_id` of `<script>` is HTML-escaped in heatmap/sparkline labels.

**Verification:** `manage.py test tests.test_metadata_dashboard_endpoints` green (Docker); renders for populated, empty, and pre-backfill contexts.

---

- U4. **Non-destructive backfill (drain-by-wait) + deploy/Make wiring**

**Goal:** A re-runnable backfill that derives the new items from existing per-stream rollups via SET, run only while ingestion is verifiably paused (rule disabled, queues drained to empty).

**Requirements:** R6, R8.

**Dependencies:** U1.

**Files:**
- Create: `cluster_management/cdk/backfill_participant_daily.py` (boto3; enumerate per-stream rollup items directly — `--study <object_id>` list preferred, admin `Scan` fallback; aggregate per (study, patient, day) → SET participant-daily; MIN day → SET `FIRST#`; ensure `CONFIG`/`SHARDS`; imports `_shard_for` from the Lambda's `dynamo_writer`; **asserts the EventBridge rule is disabled and queue+DLQ are empty before any SET**, aborts otherwise)
- Modify: `cluster_management/cdk/deploy_metadata_index.sh` (`backfill` subcommand: disable rule → **poll queue+DLQ until ApproximateNumberOfMessages + NotVisible == 0** → run the python backfill → re-enable rule; preview unless `--apply`; explicitly NOT purge)
- Modify: `Makefile` (`metadata-index-backfill` target)
- Test: `cluster_management/cdk/lambdas/metadata_index/tests/test_backfill_participant_daily.py` (import via `parents[2]`)

**Approach:**
- SET (absolute), idempotent, re-runnable **only while paused**; the rule-disabled + queues-empty assertion makes a live run abort rather than race the writer's ADDs.
- Drain-by-wait (not purge): the live writer processes in-flight uploads into the existing rollups first, so the SET reads a complete source. Document that purge is reset-only.
- Enumerate per-stream rollup items directly so rollup-only pairs (missing `LATEST#`) aren't skipped.

**Patterns to follow:** `show_metadata_index.py` (boto3 cdk-tree script); `deploy_metadata_index.sh` subcommand/preview structure (but drain-by-wait, not the reset's purge).

**Test scenarios:**
- Happy path: canned per-(patient,stream) rollups, one patient, two streams/two days → participant-daily SET values equal the per-day sums; `FIRST#` = earliest day.
- Idempotency: deriving twice yields identical SET values (absolute).
- Shard correctness: each participant's SET targets `_shard_for(patient)`.
- Multi-participant: two patients aggregate independently into their shard partitions / first-seen.
- Rollup-only pair: a (patient, stream) with rollup items but no `LATEST#` pointer is still included.
- Safety precondition: with the rule reported ENABLED (or queues non-empty), the backfill aborts before any write.

**Verification:** `pytest cluster_management/cdk/lambdas/metadata_index/tests/` green; preview prints the disable→wait→backfill→resume plan; on a paused dev table the backfilled matrix matches a direct read of the per-stream rollups; a live-rule run aborts.

---

- U5. **Docs + participant-erasure runbook (collision-safe)**

**Goal:** Document the new access patterns, the drain-by-wait backfill runbook, the backfill's minimal IAM, and extend participant erasure to the new participant-scoped items with prefix-collision-safe deletes.

**Requirements:** R6, R7.

**Dependencies:** U1, U2, U4.

**Files:**
- Modify: `cluster_management/cdk/METADATA_INDEX.md` (access-pattern table incl. the sharded aggregate, `FIRST#`, `CONFIG`/`SHARDS`; the drain-by-wait backfill runbook + minimal backfill IAM policy + that purge is reset-only; **erasure**: delete `FIRST#P#<patient>` by exact key, and `P#<patient>#DAY#` via `begins_with(SK, "P#<patient>#DAY#")` — note the trailing `#DAY#` delimiter prevents matching a longer patient id — across **all `SHARDS` partitions**; flag that the existing `LATEST#`/rollup erasure shares the prefix-collision caveat)
- Modify: `CLAUDE.md` (one line: dashboard now has adherence/enrollment views backed by a sharded participant-daily rollup + persisted `SHARDS`)

**Approach:**
- Erasure deletes use a trailing delimiter (or exact key for `FIRST#`) so erasing patient `abc` cannot match `abc123`. Loop all shards for the daily items.
- Backfill IAM: `dynamodb:Query`/`UpdateItem` on the table ARN (+`Scan` only if the Scan path is used), distinct from the reader role; document the credential source.
- Note no reader-role change (same table).

**Test scenarios:** `Test expectation: none — documentation/runbook unit.`

**Verification:** a reader can run the drain-by-wait backfill and complete a collision-safe participant erasure (incl. all shards) using only the docs.

---

## System-Wide Impact

- **Interaction graph:** Writer gains one in-saga rollup target (with a new SK shape) + an advance-earliest day pointer + a CONFIG item; reader gains `SHARDS + 2` reads incl. a loud shard-assert; template gains three widgets. Endpoint/auth unchanged.
- **Error propagation:** Participant-daily ADD is inside the existing compensation saga (now `(pk, sk)`-aware); first-seen is advance-only/idempotent; shard mismatch and read failures map to the existing loud read-error state.
- **State lifecycle risks:** The backfill is the main one — mitigated by SET-absolute + **drain-by-wait** (no purge, no lost uploads) + a rule-disabled/queues-empty precondition assert (no race) + re-runnability only while paused.
- **API surface parity:** None — same table and reader role; no Beiwe API/model changes.
- **Integration coverage:** Writer saga + shard placement + compensation SK (moto, U1); reader shard-merge/enrollment/SHARDS-assert/bounded-reads (canned, U2); template states (view test, U3); backfill aggregation + safety precondition (U4).
- **Unchanged invariants:** Existing rollups, widgets, upload path, models, Celery pipeline, reader role, and the never-`Scan` page rule are unchanged. The backfill's admin-side `Scan` (if used) is outside the dashboard read path.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Backfill purge would discard in-flight uploads from ALL rollups (silent data loss) | **Drain-by-wait, not purge**: disable rule → poll queues to 0 → SET; purge stays reset-only (R6) |
| SET races the live writer's ADD if run while ingestion is active | Backfill asserts rule disabled + queues empty before any SET; re-runnable only while paused |
| Writer/reader/backfill `SHARDS` drift → reader silently drops participants | `SHARDS` persisted in a CONFIG item; reader asserts and fails **loud**; `crc32` deterministic; backfill imports the Lambda's `_shard_for` |
| Saga compensation hits the wrong SK for the participant-daily target | `applied` + compensation loop carry `(pk, sk)` tuples; new fault test for the participant-daily ADD |
| First-seen un-derivable at sub-day precision | `FIRST#` is day-granular (`first_day`); writer + backfill agree on a date string; advance-earliest compares dates |
| Backfill misses pairs with rollup data but no `LATEST#` pointer | Enumerate per-stream rollup items directly (not via `LATEST#`) |
| Erasure prefix collision (`abc` matches `abc123`) | Exact-key delete for `FIRST#`; trailing `#DAY#` delimiter for daily items; flag the shared caveat on existing patterns |
| Adding a saga target breaks call-count fault tests | Participant-daily appended last; existing tests keep targets; add one new fault test |
| Heatmap too large for big studies / long pause + resume backlog | Day-windowed (~60d); row cap + "+M more" deferred; backfill prefers `--study` over Scan to keep the pause short; document expected pause/backlog |
| `patient_id` in new widgets (XSS) / PII in new log paths | Jinja autoescape + `{{ }}`; U1/U2 carry the no-PII-logging rule |

---

## Documentation / Operational Notes

- Rollout order: deploy writer (U1) → `metadata-index-backfill` (disable rule → **wait for queues to drain** → SET → resume) → widgets populate with history; no wipe. Re-enable produces a brief post-pause backlog the writer absorbs.
- `SHARDS` is load-bearing across writer/reader/backfill and persisted in the table; changing it requires a re-backfill and a coordinated deploy.
- CDK/Lambda + backfill tests run in the cdk venv (`pytest`+`moto`); Django reader/endpoint tests need Docker, but the reader's pure logic is host-verifiable with dummy env vars.

---

## Sources & References

- **Origin document:** [docs/plans/2026-06-24-001-feat-metadata-index-dashboard-plan.md](docs/plans/2026-06-24-001-feat-metadata-index-dashboard-plan.md)
- Collection layer: [docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md](docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md)
- Related code: `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py`, `libs/metadata_index_reader.py`, `frontend/templates/metadata_dashboard/metadata_dashboard.html`, `cluster_management/cdk/show_metadata_index.py`, `cluster_management/cdk/deploy_metadata_index.sh`, `cluster_management/cdk/METADATA_INDEX.md`
- Related PR: #3 (the Tier-1 dashboard this builds on)
