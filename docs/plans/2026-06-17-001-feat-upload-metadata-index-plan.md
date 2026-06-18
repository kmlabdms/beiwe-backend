---
title: "feat: Upload Metadata Index — event-driven S3 upload monitoring layer"
type: feat
status: active
date: 2026-06-17
---

# feat: Upload Metadata Index — event-driven S3 upload monitoring layer

## Summary

Add a self-contained AWS CDK stack that enables S3 object-created events on the raw-data bucket, buffers them through SQS into a small Python Lambda that parses **only the object key and the event record** (never the encrypted body), and writes upload facts into a DynamoDB index supporting last-upload-time, count/byte rollups, and study-level trends. The Beiwe upload path, Postgres DB, and Celery pipeline are untouched; the whole layer is `cdk destroy`-able and disable-able by removing the event source. Grafana dashboards over the DynamoDB table are a deliberate downstream consumer — this plan builds the data-collection layer and a query-friendly schema, not the dashboards.

---

## Problem Frame

The Beiwe deployment (`kowalski-beiwe`, account `838487075273`, us-east-1) writes every raw participant upload to a single S3 bucket (`beiwe-data-kowalski-beiwe-…drclz`). A coarse "last upload per participant" datum already exists — `Participant.last_upload` (Postgres) is updated on every upload — but it is participant-level only, lives in the private-VPC RDS, and carries no per-stream, byte-volume, or trend information. Answering "which *sensor* went quiet for participant X?", "is study Y's volume dropping?", or "how many bytes/files per stream per day?" today means scanning the bucket or querying the large `ChunkRegistry`/`UploadTracking` tables (RDS is `PubliclyAccessible=False` inside a private VPC, reachable only from processing servers) — expensive and operationally awkward. The genuinely-new value here is **per-stream granularity, byte/count rollups, study-level trends, and VPC-free serverless queryability**. We want a lightweight, always-current index that records one fact per uploaded object, derived passively from S3 events, with raw S3 remaining the system of record.

---

## Requirements

- R1. Record lightweight metadata for each newly uploaded raw object via S3 object-created events, with no change to the Beiwe upload path, models, or Celery pipeline.
- R2. Never read or decrypt raw object contents. All derived metadata must come from the object **key** and the **S3 event record** (size, event time, eTag).
- R3. Support these query patterns from the index:
  - last upload time per participant
  - last upload time per participant **and** data stream / sensor
  - upload counts and byte counts over time
  - study-level upload-volume trends
  - detection of **stale** streams — participants/streams that were previously active and have stopped uploading. (Detecting streams that were *expected but never uploaded* additionally requires Beiwe's expected-stream config, which is deferred — see Scope Boundaries. The index alone can only flag things it has seen.)
- R4. Treat raw S3 as the system of record; the index is a derived, rebuildable projection.
- R5. The enhancement is additive and **safe to disable** — turning it off must not affect uploads or any existing system. Tearing it down must not touch the bucket's object data or Beiwe; the only bucket-side change (the EventBridge-notifications flag) is added and removed by the stack's own managed resource (see Key Technical Decisions / U6).
- R6. Parse the real key grammar correctly, including normalization of mobile-side stream tokens and known historical irregularities; malformed/unexpected keys must be handled without data loss or crashes.
- R7. The DynamoDB schema is **designed to be Grafana-consumable** (`begins_with`-friendly keys) and will be **validated against the chosen datasource** (DynamoDB/PartiQL plugin vs. an Athena/Timestream export) before the Phase 2 Grafana work — rather than asserting zero redesign for a datasource not yet selected.

---

## Scope Boundaries

- Not building Grafana dashboards, reports, alerting, or any UI — only the data-collection layer and a query-friendly schema (R7 keeps it Grafana-ready).
- Not changing `endpoints/mobile_endpoints.py`, `libs/s3.py`, Django models, or the Celery/Forest pipeline.
- Not storing or transmitting any decrypted content, PII beyond the identifiers already present in object keys (study `object_id`, `patient_id`), or anything requiring an S3 `GetObject`.
- Not generalizing across multiple Beiwe deployments/regions beyond `kowalski-beiwe` / us-east-1.

### Deferred to Follow-Up Work

- One-time **backfill** of the index from existing bucket contents (via S3 Inventory or a paginated `ListObjectsV2` job) — future PR. The index is rebuildable (R4), but because count/byte rollups are **non-idempotent outside the dedupe-TTL window** (see Key Technical Decisions), the backfill must be a **full rebuild** (delete-then-repopulate) or run through the same per-object dedupe gate — it is **not** a safe additive merge over a live index, or overlapping objects double-count.
- **Grafana** datasource wiring and dashboards over the DynamoDB table — future work; this plan only guarantees the schema supports it.
- **Missing-data alerting** (scheduled checker that compares latest-per-stream against expected streams and emits alarms/notifications). The schema supports the query (R3); productionizing the detector + expected-stream config is deferred.

---

## Context & Research

### Relevant Code and Patterns

- `cluster_management/cdk/` — existing **Python AWS CDK** app. `app.py` wires stacks; `scheduler_stack.py` (`BeiweSchedulerStack`) is the closest pattern to mirror: Python 3.12 Lambda, `handler="index.handler"`, IAM via `add_to_role_policy`, **no VPC config**, `CfnOutput`s. `prerequisites_stack.py` shows the imported-resource / VPC conventions.
- **The authoritative key parser already exists — mirror it, don't re-derive it.** `libs/file_processing/utility_functions_simple.py:s3_file_path_to_data_type` and `database/profiling_models.py:S3File.DATA_STREAM_NAME_MAPPING` are how the running system turns an S3 key into a stream. `DATA_STREAM_NAME_MAPPING` is a **superset** of `UPLOAD_FILE_TYPE_MAPPING`: it starts from that mobile-token map, then adds canonical-name self-mappings for every `ALL_DATA_STREAMS` entry, plus the slash form `ios/log` → `IOS_LOG_FILE`, `/keys/` → `key_file`, and `forest` → `forest`. The production parser **scans path segments for a known token** rather than indexing by position. The Lambda must vendor and drift-test against this superset, not `UPLOAD_FILE_TYPE_MAPPING` alone.
- **Critical irregularities the parser must handle** (all confirmed in code): (a) `ios/log` carries a slash *inside* the stream token (`UPLOAD_FILE_TYPE_MAPPING` keys it as `ios_log` with an underscore; the slash form lives only in the superset map — comment: "we screwed up historically and can never change it"); (b) **survey** keys carry an extra `<survey_object_id>` segment *between* the stream token and the timestamp (`…/surveyTimings/<24-char id>/<ts>.csv`), so a fixed 4-segment split misreads the timestamp; (c) **audio** keys use `.mp4`/`.wav` (not `.csv`); (d) `-duplicate-<rand>` suffixed keys are normalized by `normalize_s3_file_path` (split on `-duplicate`); (e) `<study>/keys/<patient>_private.zst` RSA key files exist and must be ignored. `surveyAnswers`/`surveyTimings` are live mainline streams, so (b) is not an edge case.
- `endpoints/mobile_endpoints.py:86` — raw key body is built as `file_name.replace("_", "/")` (this is *why* `ios_log` becomes `ios/log` on S3); `libs/s3.py` prepends the study `object_id` and appends `.zst`. Confirms key grammar.

### Verified Deployment Facts (live, `shiny-dev` profile)

- Raw bucket: `beiwe-data-kowalski-beiwe-rxjfjw9miockp1gzxa42gd2zikzdrd3ndrclz`, us-east-1, **no existing event notifications** (clean slate), versioning not enabled.
- Real raw key grammar (sampled): `<study_object_id(24)>/<patient_id(≤8)>/<mobile_stream_token>/<device_timestamp_ms(13)>.csv.zst`
  - e.g. `2grtzwKjSxi64uYkxqZASgxe/7xhpe54h/accel/1779996316436.csv.zst`
  - Observed live stream tokens are mobile-side: `accel, ambientAudio, bluetoothLog, callLog, gps, gyro, logFile, powerState, surveyAnswers, surveyTimings, textsLog, wifiLog` (NOT canonical names).
  - Non-raw prefixes present in the same bucket to **exclude**: `CHUNKED_DATA/`, `LOGS/`, and (per `libs/...`) `PROBLEM_UPLOADS/`.
  - A short participant-level object exists: `<study>/<patient>.zst` (no stream segment) — a real malformed-for-our-purposes case.
- The bucket is **not managed by the CDK app** (created by the legacy `launch_script.py` deployment; `BeiwePrerequisitesStack` does not reference it). Wiring events to it is therefore a deliberate design decision (see Key Technical Decisions).
- The account already runs the target pattern: S3→Lambda→DynamoDB with a CDK `BucketNotifications` custom-resource (`KowalskiAiPrelimBaseStack`), multiple DynamoDB tables, and a Beiwe-owned Lambda (`BeiweSchedulerStack-PauseFn`, Python 3.12, no VPC). DynamoDB is the established serverless store here.

### Institutional Learnings

- None found (`docs/solutions/` and `AGENTS.md` do not exist in this repo).

### External References

- External research intentionally skipped: the account already runs the exact S3→SQS→Lambda→DynamoDB pattern and the CDK conventions are established locally. The one genuinely open external question (DynamoDB → Grafana datasource options) is captured under Open Questions / Risks rather than resolved here, since dashboards are out of scope.

---

## Key Technical Decisions

- **Store = DynamoDB (on-demand), not Beiwe Postgres.** RDS is private-VPC + `PubliclyAccessible=False`; a writer Lambda would need VPC wiring and would couple to the core DB, violating "additive / safe to disable." DynamoDB is serverless, needs no VPC, matches the account's existing pattern, and the index is rebuildable from S3 (R4). On-demand capacity absorbs bursty upload traffic with no provisioning.
- **Implementation = new Python CDK stack** `MetadataIndexStack` in `cluster_management/cdk/`, wired into `app.py` behind a CDK context flag (`enable_metadata_index`) so it is opt-in and removable (R5). Mirrors `scheduler_stack.py`.
- **Event routing = S3 → EventBridge → SQS → Lambda (recommended)**, with S3-direct-notification→SQS as the documented alternative. EventBridge is preferred because: (a) it decouples from the bucket's single `NotificationConfiguration`, so we never clobber a future notification; (b) the S3→consumer routing is a native CDK `events.Rule`; (c) disabling = delete/disable the rule, leaving the bucket flag inert (R5); (d) it allows additional future consumers (e.g., a Grafana-export pipeline) without contention. **Enabling EventBridge on the imported bucket uses the native CDK call** `s3.Bucket.from_bucket_name(...).enable_event_bridge_notification()` (verified to synthesize against an imported bucket on the pinned `aws-cdk-lib`) — this emits CDK's managed `Custom::S3BucketNotifications` resource (the same mechanism already running in-account), which reads-then-merges the existing config rather than blind-overwriting, and **removes only its own addition on `cdk destroy`**. No hand-written `AwsCustomResource` or out-of-band CLI step is needed, and teardown reverts the flag automatically.
- **SQS buffer + DLQ in front of the Lambda.** Uploads arrive in tight bursts (sampled: dozens/minute per participant during sync). SQS gives batching (cheaper DynamoDB writes), retry, back-pressure, partial-batch-failure reporting, and a dead-letter queue for poison/malformed events.
- **SQS queue policy is scoped against the confused-deputy problem.** The grant allowing `events.amazonaws.com` (EventBridge path) / `s3.amazonaws.com` (direct path) to `SendMessage` **must** carry an explicit `aws:SourceArn` condition pinning the sender to the specific EventBridge rule ARN (or bucket ARN). Without it, any principal that discovers the queue URL could inject spoofed event records and poison the index with attacker-chosen identifiers. The condition is asserted by a CDK synth test (U2), not left as narrative intent.
- **The Lambda never calls S3.** Object size, event time, eTag, bucket, and key all come from the event record. This makes "we never read raw contents" (R2) an architectural guarantee enforced by IAM (no `s3:GetObject` grant), not a convention.
- **Idempotency via a per-object dedupe item + conditional writes.** S3/SQS are at-least-once. Latest-pointer updates use a conditional "advance only if newer" write (naturally idempotent). Count/byte rollups use atomic `ADD`, which is *not* idempotent, so they are gated on the first successful conditional `PutItem` of a per-object record (`attribute_not_exists`). A TTL bounds dedupe-item storage; latest-pointers and rollups persist indefinitely. **Explicit caveat: rollups are only idempotent *within* the dedupe-TTL window.** Once a per-object item expires, a re-delivery, manual event replay, or the deferred backfill of the same key re-passes `attribute_not_exists` and **double-counts**. Therefore: set the TTL to exceed any realistic re-delivery/replay horizon; treat event replay against prod as unsafe; and require the backfill to be a full rebuild or dedupe-gated (see Scope Boundaries). A test replays an object after simulated TTL expiry and asserts the chosen behavior (U4).
- **Stream normalization map is vendored into the Lambda** (the Lambda runs without the Django app). The vendored map mirrors the production **superset** `database/profiling_models.py:S3File.DATA_STREAM_NAME_MAPPING` (mobile tokens + canonical self-maps + `ios/log`, `/keys/`, `forest`), and a repo-run test asserts parity against that exact dict — **not** `UPLOAD_FILE_TYPE_MAPPING`, which omits the `ios/log` slash form and would let the drift guard pass green while iOS logs mis-parse. The `ios/log` two-segment token is special-cased to the canonical `ios_log` before the token scan.
- **Rollup time basis = upload date (event time, UTC).** "Upload activity / volume trends / missing data" are about *when data arrived*, which is the S3 event time. The device-reported timestamp (from the filename) is stored on the per-object record too, so a device-time view is recoverable later without reprocessing.

---

## Open Questions

### Resolved During Planning

- Which bucket / region / key grammar? — Resolved by live inspection (see Verified Deployment Facts).
- Store choice (Dynamo vs Postgres)? — DynamoDB (see Key Technical Decisions).
- Where does Lambda code live and how is it tested? — Under `cluster_management/cdk/lambdas/metadata_index/`, deployed via `lambda_.Code.from_asset`; tested with **pytest + moto**, independent of the Django/Postgres test runner (these tests do not need the Beiwe app).
- How is "safe to disable" achieved? — A *new* conditional-synthesis gate in `app.py` (the stack is instantiated only when `enable_metadata_index` context is truthy; today `app.py` always instantiates its stacks, so this is a new pattern, not a mirror of existing flags). Disabling the EventBridge rule stops ingestion while preserving the table for queries; `cdk destroy MetadataIndexStack` removes the stack and the managed bucket-notification resource reverts the EventBridge flag, leaving the bucket's object data and Beiwe untouched.
- How is EventBridge enabled on the unmanaged bucket? — Native `enable_event_bridge_notification()` on the imported bucket (see Key Technical Decisions); no hand-written custom resource or manual CLI step, and teardown auto-reverts.

### Deferred to Implementation

- Exact DynamoDB attribute names, SQS batch size / window, Lambda memory/timeout, and TTL duration (must exceed the realistic re-delivery/replay horizon) — tune during implementation against observed event volume.
- Study-level daily counter hot-partition mitigation (write sharding vs deriving study totals by aggregating per-stream rollups at query time) — decide once burst volume is measured; note the same `STUDY#<study>` partition also carries every participant's latest-pointers, enlarging the exposure.

---

## Output Structure

    cluster_management/cdk/
    ├── app.py                          # MODIFY: wire MetadataIndexStack behind context flag
    ├── metadata_index_stack.py         # NEW: DynamoDB + SQS + DLQ + Lambda + event wiring
    └── lambdas/
        └── metadata_index/
            ├── handler.py              # NEW: SQS batch consumer → idempotent Dynamo writes
            ├── parser.py               # NEW: pure key/event → MetadataRecord (no AWS deps)
            ├── stream_map.py           # NEW: vendored DATA_STREAM_NAME_MAPPING superset
            ├── dynamo_writer.py        # NEW: latest-pointer + rollup + dedupe write logic
            ├── requirements.txt        # NEW: runtime deps (boto3 is in the Lambda runtime)
            └── tests/
                ├── fixtures/           # NEW: sample S3/EventBridge event JSON (+ irregularity cases)
                ├── test_parser.py      # NEW (U3)
                ├── test_stream_map_drift.py  # NEW (U3 — parity with S3File.DATA_STREAM_NAME_MAPPING)
                ├── test_handler.py     # NEW (U4, moto-backed)
                └── replay_event.py     # NEW (U5 — in-process replay against a moto table)

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```mermaid
flowchart LR
    U[Mobile upload\n(unchanged path)] -->|PutObject| S3[(Raw data bucket\nimported, unmanaged)]
    S3 -->|ObjectCreated event| EB{{EventBridge\nrule: best-effort filter\n(parser is authoritative)}}
    EB --> SQS[[SQS queue]]
    SQS -->|batch| L[Metadata writer Lambda\nno S3 access]
    L -->|conditional writes| DDB[(DynamoDB\nUploadMetadataIndex)]
    SQS -.poison/failed.-> DLQ[[DLQ]]
    L -.metrics.-> CW[CloudWatch:\nParsed/Malformed/Duplicate]
    DDB -.future.-> G[Grafana]
```

**DynamoDB single-table access patterns (PK / SK design, R3 + R7):**

| Query pattern (R3) | Item kind | PK | SK | Key attrs |
|---|---|---|---|---|
| Last upload per participant | latest-pointer | `STUDY#<study>` | `LATEST#P#<patient>` | `last_upload_time`, `last_stream`, `last_key`, `last_size` |
| Last upload per participant + stream | latest-pointer | `STUDY#<study>` | `LATEST#P#<patient>#S#<stream>` | `last_upload_time`, `last_key`, `last_size` |
| Counts / bytes over time (participant+stream) | daily rollup | `STUDY#<study>#P#<patient>#S#<stream>` | `DAY#<YYYY-MM-DD>` | `count` (ADD), `bytes` (ADD) |
| Study-level volume trend | daily rollup **(conditional)** | `STUDY#<study>` | `DAY#<YYYY-MM-DD>` | `count`, `bytes` |
| Stale-stream detection | (query) | `STUDY#<study>` | `begins_with LATEST#P#` then filter `last_upload_time < threshold` | — |

> **Study-level rollup is conditional, not committed.** Implement it as an atomic-`ADD` item initially; if observed peak write rate per study/day risks the partition's ~1000 WCU/s ceiling, switch to deriving study totals by **query-time aggregation over the per-stream rollups** instead of a dedicated counter (R3's trend query still works either way). Decide at U4 against measured burst volume — don't build the counter and then discard it without flagging this branch.
>
> **Stale vs. never-uploaded.** The stale-stream query finds participants/streams that *have* a `LATEST#` pointer older than a threshold. It **cannot** find a stream that was expected but never uploaded — that has no item to scan and needs Beiwe's expected-stream config (deferred, Phase 2).
| Idempotency / per-object log | dedupe record | `OBJ#<key>` | `OBJ` | `study, patient, stream, device_time, upload_time, size, ttl` |

`begins_with` SK prefixes (`LATEST#`, `DAY#`) keep all patterns query-friendly for the Grafana DynamoDB/PartiQL datasource (R7). The dedupe record both guarantees exactly-once counting and doubles as a queryable per-file record if needed later.

---

## Implementation Units

- U1. **Provision the DynamoDB metadata index (CDK stack scaffold + app wiring)**

**Goal:** Create `MetadataIndexStack` with the single-table DynamoDB resource (on-demand billing, TTL attribute enabled) and a least-privilege reader role, and wire it into `app.py` behind a new `enable_metadata_index` context gate (default off).

**Requirements:** R1, R4, R5, R7.

**Dependencies:** None.

**Files:**
- Create: `cluster_management/cdk/metadata_index_stack.py`
- Modify: `cluster_management/cdk/app.py`

**Approach:**
- One DynamoDB table, on-demand (`PAY_PER_REQUEST`), partition+sort key (`PK`/`SK` strings), TTL on a `ttl` attribute (used only by dedupe records).
- `removal_policy=RETAIN` so an accidental `cdk destroy` cannot drop the index. **RETAIN is an operational-safety choice, not a data-retention decision** — latest-pointers and rollups carry `patient_id`/`study_object_id` indefinitely (only dedupe records have a TTL), so participant erasure is a deliberate procedure (see U6), not something TTL satisfies.
- **Define a least-privilege reader role** (`MetadataIndexReaderRole`): `dynamodb:Query`/`GetItem` on the table ARN only (no `Scan` unless a query genuinely needs it). This is the credential the future Grafana datasource uses — the table is a full participant-upload roster and must not be read via the account-wide AdministratorAccess `beiwe-deploy` user.
- **Conditional synthesis is a new pattern**: instantiate the stack in `app.py` only when `app.node.try_get_context("enable_metadata_index")` is truthy. (Today `app.py` instantiates its stacks unconditionally and uses context only for value defaults — this gate is new, not a mirror of existing behavior.)
- `CfnOutput` the table name/ARN and reader-role ARN for the Lambda and for future Grafana wiring.

**Patterns to follow:** `cluster_management/cdk/scheduler_stack.py` (stack shape, `CfnOutput`s), `app.py` context reads.

**Test scenarios:**
- Happy path: `cdk synth -c enable_metadata_index=true` produces a template with exactly one `AWS::DynamoDB::Table` (`BillingMode: PAY_PER_REQUEST`, TTL enabled, `DeletionPolicy: Retain`) plus the reader role.
- Edge case: with the flag absent/false, the stack is not synthesized (no metadata resources in the assembly).
- Security: the reader role's policy grants only `Query`/`GetItem` on the table ARN (no `Scan`, no write actions).

**Verification:** `cdk synth` succeeds both with and without the flag; template snapshot matches the intended resource set.

---

- U2. **Provision the event-ingestion pipeline (SQS + DLQ + raw-bucket event wiring)**

**Goal:** Add an SQS main queue, a DLQ (redrive after N receives), and wire the raw bucket's `ObjectCreated` events to the queue via EventBridge — enabling notifications on the imported bucket with the native CDK call, with a source-ARN-scoped queue policy.

**Requirements:** R1, R2, R5.

**Dependencies:** U1.

**Files:**
- Modify: `cluster_management/cdk/metadata_index_stack.py`

**Approach:**
- Import the existing bucket by name (`s3.Bucket.from_bucket_name`) — do **not** create or take ownership of it — and call `.enable_event_bridge_notification()` on it. CDK emits the managed `Custom::S3BucketNotifications` resource (merges, doesn't clobber; reverts its own addition on destroy). No hand-written custom resource, no manual CLI step.
- An `events.Rule` matching `source: aws.s3`, `detail-type: "Object Created"`, `bucket.name = <raw bucket>`, targeting the SQS queue. Use an **inclusion** event pattern where possible (raw uploads only) rather than relying solely on prefix-exclusion: EventBridge `anything-but: {prefix: …}` takes a single prefix, so multi-prefix AND-exclusion of `CHUNKED_DATA/`/`LOGS/`/`PROBLEM_UPLOADS/` is not reliably expressible in one rule. Treat the rule filter as best-effort and the **parser (U3) as the authoritative gate** — and size SQS/Lambda for the possibility that all bucket writes (including high-volume `CHUNKED_DATA/`) reach the queue. (Confirm the exact pattern syntax with `cdk synth` + a live event before considering U2 done.)
- **SQS queue policy** grants EventBridge (`events.amazonaws.com`) `SendMessage` **with an explicit `aws:SourceArn` condition pinning the rule ARN** (confused-deputy guard). DLQ wired via `dead_letter_queue` with `max_receive_count`.
- Alternative path (document in code comments): `bucket.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.SqsDestination(queue))` — simpler but mutates the bucket's single notification config (clobber risk) and uses a bucket-ARN source condition instead.

**Patterns to follow:** `prerequisites_stack.py` (importing/referencing existing resources); the in-account `BucketNotifications` custom-resource pattern that CDK's native call reuses.

**Test scenarios:**
- Happy path: synthesized template contains an SQS queue + DLQ with redrive policy, an `AWS::Events::Rule`, and the `Custom::S3BucketNotifications` resource for the imported bucket.
- Security: the SQS queue policy statement for EventBridge carries an `aws:SourceArn` condition equal to the rule ARN (assert present in the synthesized template).
- Edge case: the imported bucket is referenced by name only — template contains no `AWS::S3::Bucket` resource (we never create/own it).
- Error path: DLQ redrive policy present with a finite `maxReceiveCount`.

**Verification:** `cdk synth` shows the queue/DLQ/rule; a manual `aws s3 cp` of a tiny object to a test prefix lands a message on the queue (post-deploy smoke check, documented in U6).

---

- U3. **Pure key/event parser + stream normalization (the testable core)**

**Goal:** A dependency-free module that turns `(bucket, key, size, event_time, etag)` into a normalized `MetadataRecord` or a typed "ignore"/"malformed" outcome — mirroring the **production** parser (`libs/file_processing/utility_functions_simple.py:s3_file_path_to_data_type`, `S3File.DATA_STREAM_NAME_MAPPING`) so the index can't diverge from how Beiwe itself reads keys. Handles the real grammar's irregularities: token-scan (not fixed positions), the survey `<survey_object_id>` segment, the `ios/log` slash, `-duplicate-` suffixes, non-`.csv` audio extensions, `/keys/` files, non-raw prefixes, and short keys.

**Requirements:** R2, R3, R6.

**Dependencies:** None (pure Python; can be built in parallel with U1/U2).

**Files:**
- Create: `cluster_management/cdk/lambdas/metadata_index/parser.py`
- Create: `cluster_management/cdk/lambdas/metadata_index/stream_map.py` (vendors the `DATA_STREAM_NAME_MAPPING` superset)
- Test: `cluster_management/cdk/lambdas/metadata_index/tests/test_parser.py`
- Test: `cluster_management/cdk/lambdas/metadata_index/tests/test_stream_map_drift.py` (moved here from U5 — depends only on `stream_map.py` + repo constants, so the drift guard exists the moment the map does)

**Approach:**
- URL-decode the key first (S3/EventBridge keys are URL-encoded; spaces → `+`).
- **Classify by prefix/segment → `Ignore`:** keys under `CHUNKED_DATA/`, `LOGS/`, `PROBLEM_UPLOADS/`, and keys whose second segment is `keys` (RSA key files `<study>/keys/<patient>_*.zst`) or that contain a `forest` artifact token.
- **Decide on `identifiers` explicitly:** `identifiers` is a valid stream but is flagged "not processed through data upload" in the constants. Index it as a normal `MetadataRecord` (it *is* a participant upload event) — state this choice in the module so it's deliberate, not accidental.
- Strip a trailing `-duplicate-<rand>` suffix (`normalize_s3_file_path` precedent) and the `.zst` extension before token work; keep the full original key for `last_key`/dedupe.
- **Do not assume a fixed segment count.** Take `study = segments[0]`, `patient = segments[1]`; **scan the remaining segments for a known stream token** (special-casing the two-segment `ios/log` → canonical `ios_log` first); take the **device timestamp from the leading digits of the final segment regardless of extension** (`.csv`, `.mp4`, `.wav`). This correctly handles survey keys (`…/surveyTimings/<survey_object_id>/<ts>…`) where the survey id sits between stream and timestamp.
- Normalize the stream via the vendored superset map (`stream_map.py`); unknown token → `Malformed(reason="unknown_stream")` (metered, never crashes).
- Parse the timestamp tolerantly (10-digit epoch-s and 13-digit epoch-ms); on failure keep `device_time=None` but still emit the record (upload_time/size are load-bearing).
- Validate study id (24 `[a-zA-Z0-9]`) and patient id (≤8 `[1-9a-z]`); on mismatch → `Malformed`.

**Technical design:** *(directional)* `parse(record) -> ParsedOutcome` where `ParsedOutcome ∈ {MetadataRecord, Ignore, Malformed(reason)}`. No boto3, no I/O — fully unit-testable.

**Patterns to follow:** `libs/file_processing/utility_functions_simple.py:s3_file_path_to_data_type` and `database/profiling_models.py:S3File.DATA_STREAM_NAME_MAPPING` (the authoritative token-scan + superset map); `resolve_survey_id_from_file_name` (proof the survey id is the second-to-last segment).

**Test scenarios:**
- Happy path: one valid key per observed mobile token (`accel, gps, surveyAnswers, …`) → correct `study/patient/canonical_stream/device_time_ms/size`.
- Edge case (survey, 5-segment): `<study>/<patient>/surveyTimings/<24-char survey id>/<ts>.csv.zst` → stream `survey_timings`, timestamp from the final segment, **not** Malformed.
- Edge case (`ios/log`): `<study>/<patient>/ios/log/<ts>.csv.zst` → stream `ios_log`, not `ios`.
- Edge case (audio): `<study>/<patient>/voiceRecording/<ts>.mp4.zst` → stream `audio_recordings`, timestamp parsed despite `.mp4`.
- Edge case (`-duplicate-`): `…/<ts>.csv-duplicate-<rand>.zst` → timestamp parsed from the normalized key.
- Edge case (timestamps): 13-digit ms and 10-digit s both parse; non-numeric → record with `device_time=None`.
- Edge case (short key): `<study>/<patient>.zst` → `Malformed(reason="too_few_segments")`.
- Edge case (ignored): `CHUNKED_DATA/…`, `LOGS/…`, `PROBLEM_UPLOADS/…`, and `<study>/keys/<patient>_private.zst` → `Ignore` (not Malformed — so they don't trip the Malformed alarm).
- Edge case (URL-encoding): a key with `+`/`%XX` decodes correctly before splitting.
- Error path: unknown stream token → `Malformed(reason="unknown_stream")`; invalid study/patient id → `Malformed`.
- Drift guard: `test_stream_map_drift` fails if the vendored map diverges from `S3File.DATA_STREAM_NAME_MAPPING`.

**Verification:** `pytest cluster_management/cdk/lambdas/metadata_index/tests/test_parser.py` green; every branch (ignore/valid/malformed) covered; drift test green against current constants.

---

- U4. **Metadata writer Lambda (SQS batch → idempotent DynamoDB writes + metrics)**

**Goal:** The Lambda handler: consume an SQS batch, parse each record (U3), perform idempotent DynamoDB writes (dedupe gate → conditional latest-pointer advance → rollup `ADD`), emit CloudWatch metrics, route malformed/failed records to the DLQ via partial-batch-failure reporting. Define the Lambda + its IAM in the stack.

**Requirements:** R1, R2, R3, R5, R6.

**Dependencies:** U1, U2, U3.

**Files:**
- Create: `cluster_management/cdk/lambdas/metadata_index/handler.py`
- Create: `cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py`
- Create: `cluster_management/cdk/lambdas/metadata_index/requirements.txt`
- Modify: `cluster_management/cdk/metadata_index_stack.py` (Lambda construct, SQS event source w/ `report_batch_item_failures`, IAM grants, env vars)
- Test: `cluster_management/cdk/lambdas/metadata_index/tests/test_handler.py`

**Approach:**
- Handler unwraps the SQS→(EventBridge|S3) envelope to the underlying object record(s), calls `parse`, and for `MetadataRecord`:
  1. Conditional `PutItem` of `OBJ#<key>` with `attribute_not_exists(PK)` + TTL. On `ConditionalCheckFailed` → duplicate: increment `Duplicate` metric, skip rollups, but still run the latest-pointer advance (idempotent).
  2. Latest-pointer `UpdateItem` for `LATEST#P#<patient>` and `LATEST#P#<patient>#S#<stream>` with condition `attribute_not_exists(last_upload_time) OR last_upload_time < :t` (advance-only).
  3. On first-write only: `ADD` to participant+stream daily and study daily rollups.
- `Ignore` → drop silently (count metric). `Malformed` → emit `Malformed` metric + structured log; do **not** fail the batch item (it would just poison the queue) — record it and move on, but raise/return-as-failure only for *transient* errors (throttling, dependency failure) so SQS retries and eventually DLQs genuine poison. **Keep malformed logs minimal** — prefer the stream token + segment count + a key suffix over the full key, so participant identifiers aren't broadcast into CloudWatch beyond what diagnosis needs.
- Use `report_batch_item_failures` so only failed records are retried, not the whole batch.
- IAM: `dynamodb:PutItem/UpdateItem/Query` on the table ARN; `sqs:ReceiveMessage/DeleteMessage/GetQueueAttributes` on the queue; `cloudwatch:PutMetricData` (namespace-scoped); logs. **No `s3:*`** — enforces R2.
- Lambda: Python 3.12, `Code.from_asset`, no VPC (matches `scheduler_stack.py`); env vars for table name, metric namespace, TTL days.

**Technical design:** *(directional)* write order is dedupe-gate → latest-advance → rollup-add, so a duplicate updates nothing it shouldn't and a retry is safe at every step.

**Patterns to follow:** `scheduler_stack.py` Lambda + `add_to_role_policy` IAM; the account's existing SQS→Lambda consumers.

**Test scenarios:**
- Happy path: a batch of N distinct valid objects → N dedupe items, correct latest-pointers, rollup `count==N` and `bytes==sum(sizes)` (moto-backed).
- Idempotency: replaying the same object twice → `count` stays 1, `Duplicate` metric incremented, latest-pointer unchanged.
- Idempotency boundary (TTL expiry): replaying the same object **after** its dedupe item has expired → assert the chosen behavior (documents the double-count exposure; confirms TTL choice exceeds the realistic replay horizon).
- Edge case (out-of-order): an older event after a newer one does not regress the latest-pointer; ties on `event_time` resolve deterministically (e.g., secondary compare on device_time/key) so `last_key` is reproducible regardless of arrival order.
- Integration: a single SQS event containing the real EventBridge envelope is unwrapped and written end-to-end (not just the inner record).
- Error path (malformed): malformed record is logged + metered and does **not** fail the batch (no infinite retry).
- Error path (transient): simulated DynamoDB throttling returns the record in `batchItemFailures` so SQS retries.
- Edge case: mixed batch (valid + ignore + malformed + duplicate) produces the correct per-kind outcomes.

**Verification:** `pytest .../tests/test_handler.py` green against moto; IAM policy in the synthesized template contains no `s3:` actions.

---

- U5. **Test fixtures + S3 event replay harness**

**Goal:** A library of realistic S3/EventBridge event fixtures and a replay script so events can be exercised locally and against a deployed Lambda, including malformed cases.

**Requirements:** R6 (testability), R3.

**Dependencies:** U3, U4. (The `test_stream_map_drift.py` guard lives in U3, not here — it depends only on `stream_map.py` so it exists before the writer.)

**Files:**
- Create: `cluster_management/cdk/lambdas/metadata_index/tests/fixtures/` (valid-per-stream, **survey 5-segment**, `ios/log`, **audio `.mp4`/`.wav`**, **`-duplicate-` suffixed**, `identifiers`, short-key, `CHUNKED_DATA`/`LOGS`/`PROBLEM_UPLOADS`/**`keys/`**, duplicate-delivery, URL-encoded, batch)
- Create: `cluster_management/cdk/lambdas/metadata_index/tests/replay_event.py`

**Approach:**
- Fixtures captured from the real envelope shape (EventBridge "Object Created" wrapped in SQS) so tests exercise the same unwrapping the Lambda does in prod, plus one fixture per known key irregularity.
- `replay_event.py` invokes the handler **in-process against a moto table** only. (No live `aws lambda invoke`/SQS-send mode — the deployed-stack smoke check lives solely in the U6 runbook, so the test harness needs no AWS credentials or a live stack.)

**Patterns to follow:** existing `tests/` conventions for fixture-driven tests, adapted to standalone pytest (no Django DB).

**Test scenarios:**
- Happy path: replaying each fixture through the handler yields the expected DynamoDB state.
- Integration: the SQS-wrapped EventBridge fixture replays identically to a hand-built record.
- Coverage: every irregularity fixture (survey, ios/log, audio, duplicate-suffix, keys/) resolves to the expected outcome.
- Edge case: a corrupt/non-JSON message body is reported as a batch failure, not a crash.

**Verification:** full `pytest cluster_management/cdk/lambdas/metadata_index/` suite green; drift test passes against current constants.

---

- U6. **Operational notes, disable/teardown runbook, and metrics/alarms**

**Goal:** Document deploy, the post-deploy smoke check, how to disable vs tear down (R5), what metrics/alarms exist, and the deferred backfill — so operators can run and reason about the layer.

**Requirements:** R4, R5.

**Dependencies:** U1–U5.

**Files:**
- Modify: `cluster_management/cdk/README.md` (or create `cluster_management/cdk/METADATA_INDEX.md`)
- Modify: `CLAUDE.md` (one-paragraph pointer under Deployment, noting the layer is additive and how to disable)

**Approach:**
- Deploy: `cdk deploy MetadataIndexStack -c enable_metadata_index=true` (profile/region noted).
- Smoke check: copy a tiny object to a throwaway raw-shaped prefix, confirm a queue message and a DynamoDB item; then delete it.
- **Disable** (keep data): disable/delete the EventBridge rule (ingestion stops; table preserved for queries). **Teardown**: `cdk destroy MetadataIndexStack` — the managed bucket-notification resource reverts the EventBridge flag; the table is `RETAIN`, so document the explicit table-delete step if a full wipe is intended. Neither touches the bucket's object data or Beiwe.
- **Participant erasure runbook** (IRB/withdrawal): document how to delete a participant's records — the `LATEST#P#<patient>` and `LATEST#P#<patient>#S#*` pointers, the `STUDY#<study>#P#<patient>#S#*` rollups, and any extant `OBJ#…` dedupe items for that patient — and how to confirm the deletion is complete. Note that the dedupe-record TTL does **not** satisfy erasure (latest-pointers/rollups have no TTL).
- **Reader access:** the future Grafana datasource (and any human/tool reader) authenticates as `MetadataIndexReaderRole` (U1), not the account-wide `beiwe-deploy` AdministratorAccess user. Set a finite **CloudWatch log-group retention** on the Lambda (e.g., aligned with the dedupe TTL) so participant identifiers in diagnostic logs don't persist unbounded.
- CloudWatch alarms: DLQ depth > 0, Lambda error rate, `Malformed` metric spike. Document but keep alarm creation minimal/optional.
- Note the deferred backfill and Grafana datasource as follow-ups.

**Test scenarios:** `Test expectation: none — documentation/runbook unit, no behavioral code.`

**Verification:** a reviewer can deploy, smoke-test, disable, and tear down using only the runbook.

---

## System-Wide Impact

- **Interaction graph:** Zero coupling to the Beiwe Django app — no shared code import at runtime (the parser vendors its own map). The only external surface touched is the raw bucket's **notification/EventBridge configuration** (additive, currently empty).
- **Error propagation:** Parse/poison errors are isolated per SQS record via partial-batch-failure; genuine poison lands in the DLQ; transient DynamoDB errors retry. No path can affect uploads.
- **State lifecycle risks:** At-least-once delivery handled by the dedupe-gate + advance-only latest-pointers; rollups only increment on first write. TTL bounds dedupe-item growth.
- **API surface parity:** None — no Beiwe API or model changes.
- **Integration coverage:** The SQS→EventBridge envelope unwrapping is covered by an integration-style fixture (U4/U5), not just unit parsing.
- **Unchanged invariants:** `endpoints/mobile_endpoints.py`, `libs/s3.py`, all Django models, the Celery/Forest pipeline, and the bucket's object data are explicitly unchanged. Raw S3 remains the system of record; the index is a derived projection.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Wiring events to an **unmanaged** bucket could clobber a future notification config | Native `enable_event_bridge_notification()` uses CDK's managed resource, which merges (not overwrites) and reverts only its own addition on destroy; EventBridge config is separate from `NotificationConfiguration` |
| **Parser mis-reads real key shapes** (survey 5-segment, `ios/log` slash, audio `.mp4`, `-duplicate-`, `/keys/`) — silent data loss or index pollution | Mirror the production parser (token-scan, final-segment timestamp, superset map); dedicated fixture + test per irregularity; unknown tokens → `Malformed` (metered), non-uploads → `Ignore` |
| **Drift guard validates the wrong dict**, passing green while the parser mis-handles `ios/log`/`/keys/` | Vendor and assert parity against `S3File.DATA_STREAM_NAME_MAPPING` (the superset the production parser uses), not `UPLOAD_FILE_TYPE_MAPPING` |
| **Rollups double-count** on post-TTL re-delivery, event replay, or backfill | Rollups gated on first per-object write; TTL set beyond the realistic replay horizon; replay-against-prod treated as unsafe; backfill must be a full rebuild or dedupe-gated; TTL-expiry test |
| Device-time vs upload-time confusion in downstream queries | Distinct attribute names; rollups keyed on upload date; device time retained on per-object record; deterministic tie-break on equal event_time |
| DynamoDB study-level daily counter hot partition under bursts (shared `STUDY#<study>` PK also holds latest-pointers) | On-demand billing; study-level rollup is conditional — derive totals from per-stream rollups at query time if write rate is high (deferred decision) |
| **Index read access uncontrolled** — table is a full participant-upload roster | Least-privilege `MetadataIndexReaderRole` (Query/GetItem only); Grafana uses it, not the AdministratorAccess deploy user; minimal-PII logging + log retention |
| **No participant-erasure path** (RETAIN + no TTL on identifiers) for IRB/withdrawal | Erasure runbook in U6; RETAIN documented as ops-safety, not retention policy |
| EventBridge cannot reliably exclude multiple prefixes in one rule | Parser is the authoritative filter; size SQS/Lambda for full bucket-write volume; confirm pattern syntax with `cdk synth` + live event |
| Per-object dedupe items inflate table size at scale | TTL on dedupe records; latest-pointers + rollups (the durable data) are bounded by participant×stream×day |
| DynamoDB is not a native Grafana datasource | Out of scope here; schema uses `begins_with`-friendly keys; validate against the chosen datasource (DynamoDB/PartiQL plugin or Athena/Timestream export) before Phase 2 |
| S3 event keys are URL-encoded | Parser URL-decodes first; covered by a test |
| Spoofed event injection via the SQS queue | `aws:SourceArn`-scoped queue policy (rule/bucket ARN); synth test asserts the condition |

---

## Phased Delivery

### Phase 1 (this plan) — data collection
- U1–U6: store, pipeline, parser, writer, tests, runbook. Delivers a populated, query-ready index from go-forward uploads.

### Phase 2 (deferred) — consumption & completeness
- One-time backfill from existing bucket contents (S3 Inventory).
- Grafana datasource + dashboards over the table.
- Missing-data detector/alerting using expected-stream config from Beiwe.

---

## Documentation / Operational Notes

- Lambda tests run via **standalone pytest + moto**, not the Django test runner — call this out so contributors don't try to run them under `manage.py test` (which needs Postgres and the Beiwe app).
- Deploy/disable/teardown and smoke-check steps live in the U6 runbook; the layer is opt-in via the `enable_metadata_index` context flag and leaves the bucket and Beiwe untouched when removed.

---

## Sources & References

- Live deployment inspection via `shiny-dev` profile (bucket, keys, notifications, Lambdas, CFN stacks).
- Related code: `cluster_management/cdk/scheduler_stack.py`, `cluster_management/cdk/app.py`, `cluster_management/cdk/prerequisites_stack.py`, `constants/data_stream_constants.py`, `endpoints/mobile_endpoints.py:86`, `libs/s3.py`, `database/profiling_models.py`.
