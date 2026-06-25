---
title: "feat: In-Beiwe Upload Metadata Index dashboard (per-study, read-only)"
type: feat
status: completed
date: 2026-06-24
deepened: 2026-06-24
origin: docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md  # Phase 2 consumer of that layer
---

# feat: In-Beiwe Upload Metadata Index dashboard (per-study, read-only)

## Summary

Add a researcher-facing, per-study page inside the Beiwe Django web app that visualizes the
Upload Metadata Index by querying its DynamoDB table per study (`begins_with` on SK, **never
`Scan`**), reusing the existing study-access auth and the `show_metadata_index.py` aggregation
logic. The footprint stays additive: new self-contained modules (`libs/metadata_index_reader.py`,
`endpoints/metadata_dashboard_endpoints.py`), a new Jinja2 template, a nav link, ~4 lines in
`urls.py`, and a few opt-in settings. One change lands in the **additive collection stack** (not
Beiwe core): a study-level per-stream daily rollup added to the writer, applied via a one-time
table reset (the index data is dev/disposable, confirmed by the product owner) — so per-stream
study totals are a bounded per-stream read instead of an unbounded per-participant fan-out.

---

## Problem Frame

The completed collection layer (see origin: `docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md`)
populates a DynamoDB index of upload metadata but has **no consumer** — that plan deliberately
deferred "Grafana datasource + dashboards over the table" to Phase 2. After evaluating Grafana
(the official DynamoDB datasource is a paid Grafana Enterprise plugin on Amazon Managed Grafana,
needs a PartiQL IAM action the reader role lacks, and maps awkwardly to a single-table composite-key
schema), the chosen presentation is an in-app per-study page: it reuses Beiwe's researcher login and
per-study permission model for free, adds no managed-service cost, and keeps the data layer modular.
The constraint is to touch Beiwe core minimally — the value of "in Beiwe" comes from coupling to its
auth/study model, and nothing more.

This page is **upload-activity monitoring** (raw upload arrival), deliberately distinct from Beiwe's
existing data-quantity dashboard (`endpoints/data_page_endpoints.py:dashboard_page`, driven by
`SummaryStatisticDaily` in Postgres, which measures *processed* data post-Celery). The two will show
different numbers for the same stream/day by design (raw-upload UTC arrival vs. processed
study-timezone day); the plan addresses how to keep that distinction legible (Documentation Notes).

---

## Requirements

- R1. Render a per-study upload-monitoring page in the Beiwe web app, scoped so a researcher sees only studies they have access to (reuse `@authenticate_researcher_study_access`).
- R2. Read the metadata index **only** via per-study DynamoDB `Query` (`PK = STUDY#<object_id>`, `begins_with` on SK). No `Scan`, ever. Reads must use the least-privilege `MetadataIndexReaderRole`, not the web app's broad credentials.
- R3. Present these views: (a) per-participant + per-stream **freshness** — last-upload time + stale flag (from the `LATEST#` pointers; **not** cumulative per-(participant,stream) byte/count totals, which have no bounded read — see Key Technical Decisions), (b) daily upload-volume trend for the study, (c) a recently-active feed, (d) per-stream totals/trend, (e) top-line stats (participants, streams, uploads, bytes, activity window).
- R4. Per-stream **study** totals must be a bounded read (independent of participant count) — achieved by a study-level per-stream daily rollup added to the writer (decision B from planning). Per-participant cumulative volume is intentionally not rendered (it would require the rejected fan-out).
- R5. Keep Beiwe-core changes additive and revertable: new modules + template + nav link + ~4 `urls.py` lines + opt-in settings. No changes to `mobile_endpoints.py`, `libs/s3.py`, Django models, or the Celery pipeline.
- R6. Degrade gracefully across three distinct states: **not configured/disabled** (the layer is opt-in), **empty** (no data yet), and **read error** (DynamoDB throttle, assume-role failure) — each a clear, non-500 page state; the nav link hidden when disabled.
- R7. The schema change is applied while the index data is disposable (reset + redeploy), avoiding the non-idempotent-rollup backfill problem the origin plan documents — using an ordered drain so the reset itself does not double-count in-flight events.
- R8. The read path must work end-to-end only after an explicit, verifiable IAM grant; until then the page must fail **loudly** (a distinct "cannot reach the index" state), never silently look like "disabled."

---

## Scope Boundaries

- Not building Grafana / CloudWatch / Athena / QuickSight dashboards (separate, already-evaluated decision).
- Not adding a DynamoDB GSI or a per-object "recent feed" index — the recently-active feed is derived from `LATEST#` pointers, no schema cost (decision: skip A).
- Not rendering per-(participant,stream) cumulative byte/count totals — that requires the unbounded participant×stream fan-out decision B exists to avoid (R4).
- Not changing the upload path, `libs/s3.py`, Django models, or the Celery/Forest pipeline.
- Not implementing the origin plan's deferred backfill, missing-data alerting, or expected-stream config.
- Not adding a client-side charting library — rendering is server-side to match the existing dashboard convention (revisit only if richer interactivity is later required).
- Not building a cross-study / ops "fleet health" view — this page is per-study and researcher-facing; cross-study monitoring stays with `show_metadata_index.py` / ad-hoc queries (see the audience note in Open Questions).
- Not generalizing beyond the single `kowalski-beiwe` / us-east-1 deployment.

### Deferred to Follow-Up Work

- A device-time (vs. upload-time) view, CSV export of the dashboard data, and study-comparison/cross-study views — possible later iterations, out of scope now.
- Granting the web app's IAM principal `sts:AssumeRole` is applied via whatever manages that principal (documented + verified in U5), but the policy edit itself is outside this repo.

---

## Context & Research

### Relevant Code and Patterns

- `endpoints/data_page_endpoints.py:dashboard_page(request, study_id: int)` — the page to mirror: decorators `@require_http_methods(["GET","POST"])` + `@authenticate_researcher_study_access`; re-fetches `study = get_object_or_404(Study, pk=study_id)`; passes `dict(study=..., study_id=..., page_location='dashboard_landing', ...)`; renders templates under `frontend/templates/dashboard/`. This is also the **existing data-quantity dashboard** the new page must be distinguished from.
- `authentication/admin_authentication.py:authenticate_researcher_study_access` (line ~221) — pulls `study_id` from URL kwargs/POST, validates existence + the researcher's `StudyRelation`, aborts 404/403 on failure, and sets `request.session_researcher`. Does **not** inject a `Study`; the view re-fetches it.
- `database/common_models.py:value_get` + `libs/s3.py:get_just_prefix` (line ~88) — `Study.value_get("object_id", pk=study_id)` is the established mapping from integer `study_id` to the 24-char `object_id` (used in DynamoDB `STUDY#<object_id>` keys). Returns a single field; existence is already guaranteed by the decorator in this flow.
- `libs/s3.py` (lines ~42–59) — boto3 client convention: module-global client built from `BEIWE_SERVER_AWS_*` keys + `S3_REGION_NAME`, and the `RUNNING_TESTS` → `conn = MagicMock()` test shim to mirror. **Caveat:** these are static keys that never expire — so this pattern does *not* itself cover STS-temp-credential refresh (see Key Technical Decisions).
- `config/settings.py` — `from os import getenv`; settings declared at module top. Patterns: optional-with-default, bool flag (`getenv("X","false").lower()=="true"`), typed int.
- `config/jinja2.py` / Django `TEMPLATES` `environment` — the Jinja2 environment (Django's Jinja2 backend defaults `autoescape=True`); also where a template global (e.g. `METADATA_INDEX_ENABLED`) would be injected so the navbar can see it without threading it through every view.
- `cluster_management/cdk/show_metadata_index.py:aggregate()` / `render()` — the working aggregation + display logic to port. **Important:** its `aggregate()` parser is `Scan`-based and branches on PK shape; the new `STUDY#<study>#S#<stream>` rollup will alias its study-daily branch unless patched (see U1).
- `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py` — the writer to extend: `_claim_and_count` builds `rollup_pks` and applies/compensates atomic `ADD`s; `_stream_rollup_pk` (`STUDY#<study>#P#<patient>#S#<stream>`, the existing **participant-scoped** rollup) / `_study_pk` are the key builders to model the new one on. Existing compensation tests inject failures by `_add_rollup` call-count, so adding a rollup shifts those indices.
- `cluster_management/cdk/metadata_index_stack.py` (lines ~78–84, 165) — `ReaderRole` (`dynamodb:Query`+`GetItem`, no `Scan`/writes), trusts `AccountRootPrincipal` (account-wide assume surface — see security decision), ARN exported as `ReaderRoleArn`.
- `frontend/templates/base.html` (blocks `head`/`content`/`javascript`) and `frontend/templates/navbar.html` (study-scoped link block, ~lines 98–135; "Go To Dashboard" link is the copy target; uses `easy_url(...)` and `page_location`).
- `tests/test_data_page_endpoints.py` + `tests/common.py:ResearcherSessionTest` + `tests/helpers.py` — test conventions: `ENDPOINT_NAME` class attr (enforced to match the endpoint module by `test_has_valid_endpoint_name_and_is_placed_in_correct_file`), `set_session_study_relation(ResearcherRole.researcher)`, `smart_get_status_code(200, str(study.id))`, `assert_present`. No `moto` in the Django suite — mock DynamoDB with `MagicMock`/`patch`.

### Institutional Learnings

- None — `docs/solutions/` is empty and there is no `AGENTS.md` learnings store (CLAUDE.md carries architecture guidance, already incorporated).

### External References

- Skipped by decision: strong local patterns (existing dashboard, boto3 usage, Jinja2 templates, study-access decorator) and `show_metadata_index.py` as a working DynamoDB-read reference. The Grafana datasource question was resolved earlier and is out of scope.

---

## Key Technical Decisions

- **Presentation = in-Beiwe per-study page**, reusing `@authenticate_researcher_study_access` so per-study permission scoping is free. Rationale: avoids Managed-Grafana Enterprise-plugin cost/IAM friction and gives researchers self-service via their existing login.
- **Freshness view shows last-upload + stale flag only; per-(participant,stream) cumulative totals are not rendered.** Those totals live in the participant-scoped `STUDY#<study>#P#<patient>#S#<stream>` rollups, readable only by Querying each pair (the unbounded fan-out). The `LATEST#` pointers carry `last_upload_time`/`last_key`/`last_size`/`last_stream` — enough for freshness + stale flags + the recently-active feed in a single Query — but not cumulative count/bytes. Rendering them would reintroduce the fan-out decision B avoids, so the freshness table deliberately omits them; cumulative volume is shown at the study level (study daily) and per-stream level (the new rollup, U1).
- **Schema decision B (apply now): add a study-level per-stream daily rollup** keyed `STUDY#<object_id>#S#<stream>` / `DAY#<YYYY-MM-DD>` (`count`/`bytes` via atomic `ADD`). This is **net-new and additive** — it does not replace the existing participant-scoped rollup (which still backs nothing the dashboard renders after the freshness clarification above, but stays for other consumers); it makes per-*stream* study totals a bounded read (≤ ~#streams Queries). Applied now via a **table reset + redeploy** because rollups are non-idempotent outside the dedupe-TTL window, so retrofitting later forces a full rebuild (origin plan's idempotency caveat). The reset is governed (U1): it is gated on the data being dev/disposable (product-owner-confirmed) and uses an ordered drain so it does not itself double-count.
- **Use a separate partition per (study, stream)** (`STUDY#<object_id>#S#<stream>`), not an overloaded `STUDY#<object_id>` sort-key range. Rationale: the `STUDY#<object_id>` partition already carries study daily rollups + every latest-pointer and is the origin plan's flagged hot-partition (guarded by `WRITE_STUDY_ROLLUP`); spreading per-stream writes across ~#streams partitions avoids worsening it.
- **Skip schema decision A (no recent-object feed index).** The recently-active feed is derived from the `LATEST#P#<patient>#S#<stream>` pointers (already fetched for freshness, sorted by `last_upload_time`) — this is a deliberate behavioral **change** from `show_metadata_index.py`'s `OBJ#`-record feed (which is not per-study-Queryable and is TTL'd), not a lift. Rationale: a true per-object feed needs a hot-partition-prone GSI on the hottest write path and is high-noise at production volume; "last upload per stream" is the better monitoring primitive.
- **Credential path = assume `ReaderRoleArn` via STS, with auto-refreshing credentials.** From the existing `BEIWE_SERVER_AWS_*` base creds, assume the reader role and build a DynamoDB client. The module-global handle must hold **refreshable** credentials (`botocore.credentials.RefreshableCredentials` / a deferred-refresh session, or re-assume on a short cache with expiry), **not** a one-time-assumed frozen client — a frozen client expires (~1h) and would `ExpiredToken`-fail silently in the long-lived gunicorn worker, violating R6. STS/`ClientError`/`NoCredentialsError` are caught and re-raised as the typed not-configured/read-error signal (sanitized — no role ARN/account/key in logs). Rationale: honors the origin design ("read via the least-privilege reader role, not the deploy user") without broadening the existing S3 user's policy; the static-key `libs/s3.py` pattern is mirrored only for the module-global + test-shim shape, not for credential lifetime.
- **Scope the `ReaderRole` trust to the web principal.** `assumed_by=AccountRootPrincipal()` lets any account principal with `sts:AssumeRole` reach the participant roster. Tighten the trust policy in `metadata_index_stack.py` to the specific web-tier principal ARN (passed as CDK context), so the trust boundary is explicit rather than account-wide (U5). The caller-side `sts:AssumeRole` grant is still required and is a verified prerequisite (R8/U5).
- **Server-side rendering, no new JS dependency.** Tables + CSS/inline-SVG bars matching `show_metadata_index.py`'s aesthetic; Jinja2 `autoescape=True` (confirm in `config/jinja2.py`) renders all `patient_id`/stream/study values escaped (XSS guard, since `patient_id` originates from an S3 key the server never sanitizes). Chart.js is a documented future option.
- **Opt-in + graceful degradation, three states.** `METADATA_INDEX_ENABLED` (+ table name / region / role ARN) gates the feature: the nav link is hidden (via a Jinja2 template global / context processor so the navbar sees the flag without per-view threading) and the view renders distinct **not-configured**, **empty**, and **read-error/cannot-assume-role** states (R6/R8) — the last is loud, not a silent "no data."
- **DynamoDB access via module-global client + test shim.** Mirror `libs/s3.py`'s module-global + `RUNNING_TESTS` `MagicMock` shape (the Django suite has no `moto`), combined with the refreshable-credential requirement above.

---

## Open Questions

### Resolved During Planning

- Presentation surface (Grafana vs. in-app)? — In-app Level-1 page (conversation decision).
- Change the schema? — Yes for per-stream **study** totals (B), now via reset; no per-object feed index (A). (Conversation decision.)
- How does the web app get DynamoDB credentials, and how is expiry handled? — Assume `ReaderRoleArn` via STS with **refreshable** credentials (Key Technical Decisions); a frozen one-time-assumed client is explicitly rejected.
- study_id → object_id? — `Study.value_get("object_id", pk=study_id)`; the reader validates it is a 24-char alnum id before building any key (U2).
- Charting library? — None; server-side render.
- Does the freshness view show per-(participant,stream) cumulative totals? — No (bounded-read boundary; see Key Technical Decisions / R3a / R4).
- Where do the view test and endpoint name live? — `endpoints/metadata_dashboard_endpoints.py` → `ENDPOINT_NAME = "metadata_dashboard_endpoints.<view>"` → test in `tests/test_metadata_dashboard_endpoints.py`.
- Is the reset safe to run? — Gated (U1): disposable data is product-owner-confirmed, plus an ordered drain (disable rule → purge queue+DLQ → reset → redeploy → re-enable).

### Deferred to Implementation

- Exact refresh mechanism within the refreshable-credentials decision (botocore `RefreshableCredentials` vs. short-TTL re-assume cache) — both satisfy R6; pick during implementation.
- Whether to expose a configurable `stale-hours` query param on the page (default 24h, surfaced in the UI either way).
- For very large studies, whether the per-participant freshness table paginates or groups (collapsible by participant) — decide against a real rendered page; the data shape (participants × streams rows) is known.

### Audience note (recorded, not a blocker)

The origin index was framed for ops "which study/stream went quiet" monitoring; this page is intentionally researcher-facing and per-study (the deliberate Level-1 product choice). A cross-study ops view is explicitly out of scope (Scope Boundaries); ops continue to use `show_metadata_index.py`. Flagged so the per-study scope is a recorded decision, not an oversight.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Request flow (all reads per-study, `Query` only):

```mermaid
flowchart LR
    R[Researcher\n(logged in)] -->|GET /metadata_dashboard/<study_id>| V[metadata_dashboard_endpoints\n@authenticate_researcher_study_access]
    V -->|Study.value_get object_id| OID[study object_id\n(validated 24-char)]
    V --> RD[libs/metadata_index_reader]
    RD -->|sts:AssumeRole ReaderRoleArn\n(refreshable creds)| STS[(STS)]
    RD -->|Query begins_with, no Scan| DDB[(DynamoDB index)]
    RD --> AGG[aggregated views]
    AGG --> T[Jinja2 template\nserver-rendered tables + CSS bars\nautoescaped]
```

DynamoDB read access patterns this page uses (per study, `PK = STUDY#<object_id>`):

| View (R3) | PK | SK condition | Notes |
|---|---|---|---|
| Daily volume trend + top-line totals | `STUDY#<object_id>` | `begins_with(SK, "DAY#")` | 1 Query |
| Participant + per-stream freshness (last-upload + stale), recently-active feed | `STUDY#<object_id>` | `begins_with(SK, "LATEST#P#")` | 1 Query; yields participants, the stream set, and the feed (sort by `last_upload_time`). **No cumulative per-(participant,stream) totals** — pointers carry only `last_upload_time`/`last_key`/`last_size`/`last_stream` |
| Per-stream **study** totals/trend | `STUDY#<object_id>#S#<stream>` | `begins_with(SK, "DAY#")` | one Query per stream in the set — **bounded** (≤ ~#streams), the payoff of decision B |

Per-page Query count ≈ `2 + #streams` (bounded, independent of participant count). Stream-set source caveat: the set is "streams with a surviving `LATEST#` pointer," which can disagree with rollup partitions after erasure/compensation — the reader treats a stream with no rollup items as zero-volume (U2).

New writer rollup (U1), applied alongside the existing ADDs (net-new partition):

| Item | PK | SK | Attrs |
|---|---|---|---|
| Study-level per-stream daily rollup (new) | `STUDY#<object_id>#S#<stream>` | `DAY#<YYYY-MM-DD>` | `count` (ADD), `bytes` (ADD) |

---

## Implementation Units

- U1. **Collection-stack schema: study-level per-stream daily rollup + governed reset/redeploy**

**Goal:** Extend the writer so each upload also `ADD`s to a `STUDY#<study>#S#<stream>` / `DAY#<day>` rollup (in the same dedupe-gate + compensation saga as the existing rollups); patch the existing scan-based viewer so the new PK doesn't corrupt it; and reset the (dev/disposable, product-owner-confirmed) table via an ordered drain so the reset itself doesn't double-count.

**Requirements:** R4, R7.

**Dependencies:** None.

**Files:**
- Modify: `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py` (add a `_study_stream_rollup_pk(study, stream)` builder; append its PK to `rollup_pks` in `_claim_and_count`)
- Modify: `cluster_management/cdk/lambdas/metadata_index/tests/test_handler.py` (assert the new rollup written/compensated; **update the call-count-based fault-injection tests** — `test_split_brain_avoided_when_second_rollup_fails`, `test_transient_rollup_failure_rolls_back` — whose `_add_rollup` failure indices shift when a third rollup is added)
- Modify: `cluster_management/cdk/show_metadata_index.py` (`aggregate()` study-daily branch currently matches any `STUDY#…`/`DAY#` PK without `#P#`; exclude `#S#` from that branch and add an explicit per-stream branch, so the ops viewer doesn't invent bogus studies or double-count after the change)
- Modify: `cluster_management/cdk/METADATA_INDEX.md` (document the new access pattern; the reset runbook; and that the new rollup is a study-level **aggregate** — like the existing study-daily rollup it cannot single out a participant, so the participant-erasure runbook's study-aggregate caveat applies to it too)

**Approach:**
- Mirror `_stream_rollup_pk`; new PK `STUDY#<study>#S#<stream>`, SK `DAY#<day>`, same `day = upload_time[:10]` (upload/event date, UTC) basis.
- Add the new PK to `rollup_pks` before the ADD loop so the existing compensate-and-unclaim-on-failure saga covers it (no new failure path); it is always written (independent of `WRITE_STUDY_ROLLUP`).
- **Ordered reset** (avoids the idempotency window): disable the EventBridge rule → purge the SQS queue **and** DLQ (drain in-flight/retryable messages) → delete + recreate the table (or delete all items) → redeploy the writer → re-enable the rule. Without the drain, queued/DLQ'd messages replayed after the wipe re-pass the `attribute_not_exists` gate and double-count.
- **Reset gate:** confirm the table is dev/disposable (product owner has confirmed) before running; the index then repopulates from go-forward uploads (no backfill). Alternative if a future reset is undesirable: deploy the writer change without reset and let the new rollup accrue forward (per-stream totals undercount history until it fills) — recorded so the reset is a deliberate choice.

**Patterns to follow:** `dynamo_writer.py:_stream_rollup_pk`/`_add_rollup`/`_claim_and_count`; the origin plan's disable/teardown runbook for the rule + queue handling.

**Test scenarios:**
- Happy path: N distinct valid objects for one (study, stream) → `STUDY#<study>#S#<stream>` / `DAY#<day>` has `count==N`, `bytes==sum(sizes)`.
- Edge case (multi-stream): two streams write two distinct per-stream partitions, each correct; the study-level `STUDY#<study>` / `DAY#` total equals the combined count.
- Idempotency: replaying the same object (within TTL) does not double-count the new rollup.
- Error path (compensation): a simulated ADD failure compensates the already-applied ADDs (including the new one) and deletes the marker, so a retry re-counts exactly once across all three rollups; the updated fault-injection tests target the correct logical ADD after the index shift.
- Regression (viewer): `show_metadata_index.py:aggregate()` over a table containing the new PK produces no bogus study and does not double-count study totals (unit test on `aggregate()` with a per-stream item present).

**Verification:** `pytest cluster_management/cdk/lambdas/metadata_index/tests/` green; the patched `show_metadata_index.py` renders correctly against a table with the new rollup; a redeployed stack writes the new rollup for a smoke-test upload; `METADATA_INDEX.md` documents the pattern, reset runbook, and erasure caveat.

---

- U2. **Read layer: `libs/metadata_index_reader.py` + settings**

**Goal:** A self-contained module that assumes `ReaderRoleArn` with **refreshable** credentials, builds a DynamoDB client, and exposes a `study_summary(study_object_id, stale_hours=24)`-style function returning the aggregated views using **only** per-study `Query` (`begins_with`), never `Scan`. Add the opt-in settings it reads.

**Requirements:** R2, R3, R4, R6, R8.

**Dependencies:** U1 (per-stream totals read the new rollup).

**Files:**
- Create: `libs/metadata_index_reader.py`
- Modify: `config/settings.py` (`METADATA_INDEX_ENABLED`, `METADATA_INDEX_TABLE_NAME`, `METADATA_INDEX_REGION`, `METADATA_INDEX_READER_ROLE_ARN`, following the existing `getenv` patterns)
- Test: `tests/test_metadata_index_reader.py`

**Approach:**
- Build a lazily-initialized module-global DynamoDB table handle from STS-assumed **refreshable** temporary credentials (assume `METADATA_INDEX_READER_ROLE_ARN` from the `BEIWE_SERVER_AWS_*` base creds + `METADATA_INDEX_REGION`). The credentials must auto-refresh (or re-assume on a short, expiry-aware cache) so a long-lived worker never serves an expired client. Under `RUNNING_TESTS`, replace/patch the handle with a `MagicMock` (mirror `libs/s3.py`).
- **Input validation:** assert `study_object_id` is a 24-char alphanumeric id before constructing any key (a corrupt/`#`-bearing value could alias the per-stream partition space); on mismatch raise the typed signal.
- Reuse the aggregation shape from `show_metadata_index.py:aggregate()` but feed per-study `Query` results, not a `Scan`:
  - Query `PK=STUDY#<obj>`, `begins_with(SK,"DAY#")` → study daily trend + totals + activity window.
  - Query `PK=STUDY#<obj>`, `begins_with(SK,"LATEST#P#")` → participant pointers + per-(participant,stream) pointers (freshness + stale flags), the stream set, and the recently-active feed (sort by `last_upload_time` desc, top 15). **Freshness rows carry last-upload time + stale flag + `last_size` only — no cumulative count/bytes.**
  - For each stream in the set, Query `PK=STUDY#<obj>#S#<stream>`, `begins_with(SK,"DAY#")` → per-stream totals/trend (bounded). A stream with a pointer but no rollup items → zero volume (don't error).
- Paginate every Query via `LastEvaluatedKey`. Never call `scan`.
- **Failure handling (R6/R8):** catch `botocore` `ClientError`/`NoCredentialsError` (assume-role failure, throttle) and missing-config; raise typed signals distinguishing **not-configured** from **read-error/cannot-assume-role**. Do **not** log `Query` responses or exception payloads that contain `patient_id`, the role ARN, account, or keys — log by reason/error-code only (mirrors the writer's minimal-PII logging).
- Empty index → zero-filled structures, not an error.

**Execution note:** Build the pure aggregation/transform path test-first against canned `Query` responses; the reader has no behavioral dependency on live AWS.

**Patterns to follow:** `libs/s3.py` (module-global + `RUNNING_TESTS` shim — shape only, not credential lifetime); `show_metadata_index.py:aggregate`/`num`/`parse_time`/`ago`; boto3 `Key('PK').eq(...) & Key('SK').begins_with(...)`.

**Test scenarios:**
- Happy path: canned `Query` responses → correct top-line totals, per-participant freshness map (last-upload + stale, no cumulative totals), per-stream study totals, feed sorted newest-first.
- Edge case (stale flags): a stream older than `stale_hours` is flagged; one inside the window is not; the threshold boundary is deterministic.
- Edge case (stream set ≠ rollup): a stream with a `LATEST#` pointer but no `STUDY#<obj>#S#<stream>` rollup items renders as zero volume, not an error.
- Edge case (empty index): all Queries return nothing → zero-filled stats, empty lists, no exception.
- Edge case (pagination): a `Query` returning `LastEvaluatedKey` is fully drained.
- Error path (not configured): `METADATA_INDEX_ENABLED` false / missing setting → typed not-configured signal, no AWS call.
- Error path (read error): `assume_role`/Query raises `ClientError` → typed read-error signal; assert the logged message contains no `patient_id`/role ARN.
- Error path (bad object_id): a non-24-char/`#`-bearing id → typed invalid-input signal, no Query issued.
- Invariant (no Scan): the mocked table's `scan` is never called.
- Bounded reads: a study with S streams issues exactly S per-stream Queries (independent of participant count).

**Verification:** `pytest tests/test_metadata_index_reader.py` green; grep confirms no `scan(`; per-page Query count is `2 + #streams`; credential object is refreshable (not a one-time-frozen client).

---

- U3. **Endpoint view + URL wiring**

**Goal:** A new endpoint module rendering the per-study page, mirroring `dashboard_page`: study-access auth, `study_id → object_id` mapping, reader call, the three distinct degradation states, template render. Register the route.

**Requirements:** R1, R3, R6, R8.

**Dependencies:** U2, U4.

**Files:**
- Create: `endpoints/metadata_dashboard_endpoints.py`
- Modify: `urls.py` (route via the `path(...)` helper, `login_redirect=SAFE`; add the module to the `from endpoints import (...)` block)
- Test: `tests/test_metadata_dashboard_endpoints.py`

**Approach:**
- Decorators `@require_http_methods(["GET"])` + `@authenticate_researcher_study_access`; signature `metadata_dashboard_page(request: ResearcherRequest, study_id: int)`.
- `object_id = Study.value_get("object_id", pk=study_id)`; `study = get_object_or_404(Study, pk=study_id)` for context.
- Call the reader; map its typed signals to three rendered states (all HTTP 200): **not-configured** ("metadata index not available in this environment"), **read-error/cannot-assume-role** (a loud "could not reach the upload index — check configuration/permissions", **no partial/zero data shown alongside it** so it can't be mistaken for "no data"), **empty** ("no data yet").
- Pass `dict(study=study, study_id=study_id, page_location='metadata_dashboard', metadata_index_enabled=..., state=..., <aggregated views>, stale_hours=...)`.
- URL: `path("metadata_dashboard/<int:study_id>", metadata_dashboard_endpoints.metadata_dashboard_page, login_redirect=SAFE)`.

**Patterns to follow:** `endpoints/data_page_endpoints.py:dashboard_page`; `urls.py` lines 72–83.

**Test scenarios:**
- Happy path: researcher with access GETs the page → 200, key sections present (reader patched with canned aggregates).
- Auth: researcher without a `StudyRelation` → 403; non-existent study_id → 404 (decorator behavior, assert for this route).
- Edge case (not configured): reader raises not-configured → 200, "not available" present, no 500.
- Edge case (read error): reader raises read-error → 200, the loud error banner present, and **no** zero-filled data tables rendered.
- Edge case (empty): reader returns zero-filled → 200, "no data yet" present.
- Security (XSS): a canned `patient_id` of `<script>alert(1)</script>` is HTML-escaped in the response (autoescape guard).
- Covers R2: assert the reader is called with the study's `object_id` (not the integer PK).
- Integration: the auto `test_has_valid_endpoint_name_and_is_placed_in_correct_file` passes for the new endpoint.

**Verification:** `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests.test_metadata_dashboard_endpoints` green; the auto endpoint-name/test-placement check passes; manual load renders for a seeded study and for each degradation state.

---

- U4. **Template + nav link**

**Goal:** A Jinja2 template (extending `base.html`) rendering all views server-side (tables + CSS/inline-SVG bars, no new JS dependency) with specified stale-flag rendering, section order, and the three states; plus a study-scoped nav link that is hidden when the feature is disabled.

**Requirements:** R3, R5, R6.

**Dependencies:** None for authoring (consumes the U3 context).

**Files:**
- Create: `frontend/templates/metadata_dashboard/metadata_dashboard.html`
- Modify: `frontend/templates/navbar.html` (study-scoped link in the `{% if study and request.session_researcher %}` block, guarded by `page_location != 'metadata_dashboard'` **and** the enabled flag)
- Modify: `config/jinja2.py` (or the `TEMPLATES` `environment`) — inject `METADATA_INDEX_ENABLED` as a Jinja2 global (or add a context processor) so the navbar can read it without threading it through every view
- Test: covered by the U3 view test (`assert_present` on section markers + the XSS assertion)

**Approach:**
- Section order (status before history, monitoring intent): (1) top-line stats row, (2) recently-active feed (top 15), (3) per-participant freshness table, (4) per-stream totals table, (5) daily volume bar chart — each under an `<h3>` so the page has a scannable outline.
- **Stale flag** rendered as a Bootstrap-3 `warning` row class plus a `<span class="label label-warning">stale &gt;Nh</span>` in the stream cell, with a small legend (fresh / stale / no data) and the active threshold surfaced (e.g. "flagged if no upload in past 24h") so researchers can interpret it.
- **States:** render the not-configured / empty / read-error states from the `state` context flag; the read-error state uses an `alert-danger` banner and shows no data tables.
- **Labeling vs. the existing dashboard:** title and nav label make clear this is **upload activity** (arrival), distinct from "Go To Dashboard" (processed data quantities). Add a one-line caveat that counts reflect raw upload arrival (UTC), not processed data, and may lag device collection time.
- Large studies: the freshness table is participants × streams rows; group rows by participant (the existing dashboard's two-column list is the styling precedent) so a 500-participant study is navigable; pagination is a deferred refinement.
- Escape all `patient_id`/stream/study values via autoescaped `{{ value }}` (never `| safe`).

**Patterns to follow:** `frontend/templates/dashboard/dashboard.html` / `data_stream_dashboard.html` (server-rendered tables, conditional cell styling); `navbar.html` "Go To Dashboard" link; `show_metadata_index.py:bar()` for the bar concept.

**Test scenarios:**
- `Test expectation: none in this unit — rendered content (section markers, stale label, three states, escaped patient_id) is asserted via the U3 view test.`

**Verification:** the page renders without template errors for populated, empty, not-configured, and read-error contexts; the nav link appears on other study pages only when enabled, and is hidden/inactive on the metadata page itself.

---

- U5. **IAM/credential wiring (loud failure) + trust scoping + operational docs**

**Goal:** Make the read path work end-to-end and fail loudly until it does: the verified `sts:AssumeRole` grant on `ReaderRoleArn`, a tightened role trust policy, the new settings, and operator docs.

**Requirements:** R2, R5, R6, R8.

**Dependencies:** U1, U2, U3.

**Files:**
- Modify: `cluster_management/cdk/metadata_index_stack.py` (scope `ReaderRole`'s `assumed_by` to the web-tier principal ARN via CDK context, instead of `AccountRootPrincipal`)
- Modify: `cluster_management/cdk/METADATA_INDEX.md` ("Reader access for the Beiwe web app": the assume-role path, the exact `sts:AssumeRole` grant the web principal needs, a **verification step** — assume the role and run one Query — and the direct-grant fallback)
- Modify: `CLAUDE.md` (one-paragraph pointer: the in-app dashboard is opt-in via `METADATA_INDEX_ENABLED` + table/region/role-ARN settings, read-only, per-study)

**Approach:**
- Document and verify that the web principal holding `BEIWE_SERVER_AWS_*` is granted `sts:AssumeRole` on `ReaderRoleArn`; with the trust scoped to that principal, both sides are explicit. Provide a copy-paste verification (assume + one Query) so an operator confirms the path before relying on the page (R8). The page's read-error state surfaces the gap loudly if the grant is missing.
- Document the four settings and that absence/`false` disables the page and hides the nav link.
- Carry the origin plan's security posture: the table is a full participant-upload roster; read it via the least-privilege role, never the broad deploy user; the new per-stream rollup is a study aggregate that the participant-erasure runbook cannot single out (rebuild caveat).

**Test scenarios:** `Test expectation: none — documentation/ops + a CDK trust-policy change verified by the existing synth tests (assert the trust principal is the scoped ARN, not account-root).`

**Verification:** `cdk synth` shows the scoped trust principal; a reader can enable the feature in a fresh environment using only these docs (set env vars, grant + verify `sts:AssumeRole`, load the page); a missing grant shows the loud read-error state, not a silent "disabled."

---

## System-Wide Impact

- **Interaction graph:** New read-only path Researcher → new endpoint → new reader lib → STS → DynamoDB. Beiwe-core touchpoints: `urls.py` (one route), `navbar.html` (one link), `config/settings.py` (opt-in settings), `config/jinja2.py` (one template global). No write paths, no model changes.
- **Error propagation:** Reader failures (not-configured, read-error, assume-role failure, throttling) surface as distinct friendly page states, never a 500; auth failures keep the decorator's 403/404 behavior.
- **State lifecycle risks:** Read-only in Beiwe. In the collection stack, the new rollup shares the existing dedupe-gate + compensation saga; the governed ordered-drain reset closes the transient idempotency window during the schema change.
- **API surface parity:** No Beiwe API/model changes. The new DynamoDB access pattern is additive and documented (U1); the ops viewer (`show_metadata_index.py`) is updated in lockstep.
- **Integration coverage:** view↔reader contract (object_id mapping, three states) covered by U3 with the reader patched; reader Query/aggregation/credential-refresh covered by U2 with canned responses; writer rollup + compensation + viewer regression covered by U1.
- **Unchanged invariants:** `endpoints/mobile_endpoints.py`, `libs/s3.py`, all Django models, the Celery/Forest pipeline, and the raw S3 upload path are unchanged. Raw S3 remains the system of record; the index is a derived projection this plan only reads (plus the additive writer rollup).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Freshness view silently drops, or unboundedly fans out for, per-(participant,stream) totals | Decision recorded (R3a/R4): freshness shows last-upload + stale only; cumulative volume at study/per-stream level; documented so it's a deliberate boundary, not a regression |
| New rollup PK corrupts the existing scan-based `show_metadata_index.py` | U1 patches `aggregate()` in lockstep + a regression test |
| Reset re-opens the idempotency window (in-flight SQS/DLQ double-count) | Ordered drain in U1: disable rule → purge queue+DLQ → reset → redeploy → re-enable |
| STS temp credentials expire under a long-lived worker → silent `ExpiredToken` | Refreshable credentials required (not a frozen client); read-error state if refresh fails (R6) |
| Feature can't read until an out-of-band IAM grant lands, and could look "disabled" | R8: loud read-error state + a verification step in U5; trust scoped to the web principal |
| `ReaderRole` trusts the whole account (`AccountRootPrincipal`) | U5 scopes `assumed_by` to the web-tier principal ARN; synth test asserts it |
| `patient_id` from an unsanitized S3 key rendered as HTML (stored XSS) | Jinja2 `autoescape=True` (confirmed in `config/jinja2.py`); render via `{{ }}` never `| safe`; U3 XSS test |
| PII (`patient_id`) leaking into logs/Sentry | Reader never logs Query responses/exception payloads; logs by reason only; `DEBUG=False` noted in U5 |
| Corrupt/`#`-bearing `object_id` aliases the per-stream partition space | U2 validates 24-char alnum before key construction |
| Per-stream fan-out grows with stream count | Bounded by the canonical stream set (≤ ~20), independent of participant count |
| Large study → very long freshness table | Group rows by participant; pagination deferred (Open Questions) |
| New per-stream rollup not covered by participant-erasure runbook | It's a study aggregate (like study-daily) and can't single out a participant; documented with the rebuild caveat (U1/U5) |
| Two adjacent "dashboard" surfaces confuse researchers / numbers differ | Distinct nav label + page title ("upload activity"), and a caveat that raw-upload UTC counts differ from processed study-tz quantities (U4/Docs) |
| Accidental `Scan` (cost/permission) | All reads are per-study `Query`; the reader role lacks `dynamodb:Scan` (the load-bearing control); U2 test + grep are belt-and-suspenders |

---

## Documentation / Operational Notes

- Django web-tier tests run under `manage.py test` (real Postgres, `MagicMock`/`patch` for DynamoDB — no `moto`); collection-stack Lambda tests stay in the CDK venv with `pytest`+`moto`. U1 touches the CDK suite; U2/U3 touch the Django suite.
- Enable per environment: set `METADATA_INDEX_ENABLED=true` + table/region/role-ARN, grant **and verify** `sts:AssumeRole` on the role ARN, ensure `DEBUG=False`.
- The schema change (U1) requires a one-time ordered-drain reset + redeploy; the index repopulates from go-forward uploads (no backfill).
- Make the relationship to the existing `SummaryStatisticDaily` dashboard explicit in the UI: this page is raw-upload arrival monitoring (UTC), distinct from processed data quantities (study timezone); the numbers are expected to differ.

---

## Sources & References

- **Origin document:** [docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md](docs/plans/2026-06-17-001-feat-upload-metadata-index-plan.md) (Phase 2 deferred this dashboard)
- Related code: `endpoints/data_page_endpoints.py`, `authentication/admin_authentication.py`, `libs/s3.py`, `config/settings.py`, `config/jinja2.py`, `database/common_models.py` (`value_get`), `cluster_management/cdk/show_metadata_index.py`, `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py`, `cluster_management/cdk/metadata_index_stack.py`, `frontend/templates/base.html`, `frontend/templates/navbar.html`, `tests/test_data_page_endpoints.py`, `tests/common.py`, `tests/helpers.py`
- Reference doc: `cluster_management/cdk/METADATA_INDEX.md`
