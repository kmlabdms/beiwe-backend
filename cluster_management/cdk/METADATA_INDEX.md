# Upload Metadata Index

An additive, event-driven layer that records lightweight metadata about each raw
upload to the Beiwe S3 bucket, so we can monitor upload activity without scanning
the bucket. It **does not** change the Beiwe upload path, read or decrypt object
contents, or touch the Postgres DB. Raw S3 remains the system of record.

```
raw S3 bucket --(ObjectCreated)--> EventBridge --> SQS (+DLQ) --> writer Lambda --> DynamoDB index
```

Defined in `metadata_index_stack.py` (`MetadataIndexStack`), wired into `app.py`
behind the `enable_metadata_index` context flag. Lambda code lives in
`lambdas/metadata_index/`.

## What the index supports

| Query | DynamoDB access pattern |
|---|---|
| Last upload per participant | `STUDY#<study>` / `LATEST#P#<patient>` |
| Last upload per participant + stream | `STUDY#<study>` / `LATEST#P#<patient>#S#<stream>` |
| Counts / bytes over time (per stream) | `STUDY#<study>#P#<patient>#S#<stream>` / `DAY#<YYYY-MM-DD>` |
| Study-level volume trend | `STUDY#<study>` / `DAY#<YYYY-MM-DD>` |
| Stale-stream detection | query `STUDY#<study>`, `begins_with LATEST#P#`, filter `last_upload_time < threshold` |
| Idempotency / per-object log | `OBJ#<key>` / `OBJ` (TTL'd) |

> Stale-stream detection finds streams that *were* active and stopped. Detecting
> streams **expected but never uploaded** needs Beiwe's expected-stream config and
> is deferred (Phase 2). Grafana dashboards are a downstream consumer (Phase 2);
> this layer only collects the data and exposes a `begins_with`-friendly schema.

## Deploy (opt-in)

The stack is **not** synthesized unless the flag is truthy.

```bash
cd cluster_management/cdk
cdk deploy MetadataIndexStack -c enable_metadata_index=true   # --profile / region as configured
# optional: -c raw_bucket_name=<bucket>   (defaults to the kowalski-beiwe raw bucket)
```

This enables EventBridge notifications on the (existing, unmanaged) raw bucket via
CDK's managed `Custom::S3BucketNotifications` resource — it **merges** with any
existing notification config rather than overwriting it.

## Smoke check (post-deploy)

1. Copy a tiny object to a raw-shaped, throwaway prefix:
   `aws s3 cp /tmp/x.zst s3://<bucket>/<study24>/zzsmoke01/gps/<unixms>.csv.zst`
2. Confirm a message flowed through the queue and a DynamoDB item appeared
   (`OBJ#…/zzsmoke01/…` and a `STUDY#…/LATEST#P#zzsmoke01` pointer).
3. Delete the test object. (It is a throwaway patient id, not real data.)

## Disable vs. teardown (both leave the bucket and Beiwe untouched)

- **Disable (keep the data):** disable or delete the EventBridge rule
  (`ObjectCreatedRule`). Ingestion stops immediately; the table is preserved for
  queries.
- **Teardown:** `cdk destroy MetadataIndexStack`. The managed bucket-notification
  resource reverts the EventBridge flag on the bucket. The DynamoDB table is
  `RemovalPolicy.RETAIN`, so it survives `destroy` — delete it explicitly if a
  full wipe is intended:
  `aws dynamodb delete-table --table-name <TableName from the stack output>`.

Neither path mutates the bucket's object data or any Beiwe resource.

## Participant data erasure (IRB / withdrawal)

`RemovalPolicy.RETAIN` is an operational-safety choice, **not** a data-retention
policy. Latest-pointers and rollups carry `patient_id`/`study_object_id`
indefinitely (only the `OBJ#` dedupe records have a TTL), so participant deletion
is a deliberate procedure, not something TTL satisfies. To erase a participant:

1. Delete the latest pointers: `STUDY#<study>` items with SK `LATEST#P#<patient>`
   and SK `begins_with LATEST#P#<patient>#S#`.
2. Delete the per-stream daily rollups: items with PK
   `begins_with STUDY#<study>#P#<patient>#S#`.
3. Delete any remaining `OBJ#<key>` dedupe records for that patient (query/scan by
   the `patient` attribute, or by key prefix `OBJ#<study>/<patient>/`).
4. Confirm none remain. Note that **study-level** `DAY#` rollups are aggregates and
   do not single out a participant; rebuild them from raw S3 if exact study totals
   must exclude the erased participant.

## Reader access (Grafana and humans)

Read the table via the least-privilege `MetadataIndexReaderRole` (the
`ReaderRoleArn` stack output): `dynamodb:Query` + `GetItem` only, no `Scan`, no
writes. Grant `sts:AssumeRole` on it to the specific Grafana datasource principal
— **do not** read the table with the account-wide AdministratorAccess deploy user.
The table is effectively a full participant-upload roster.

## Observability

- The writer emits CloudWatch metrics under the `BeiweUploadMetadata` namespace:
  `Written`, `Duplicate`, `Ignored`, `Malformed`, `CorruptBody`, `RetryableError`.
- Writer logs use a bounded retention (1 month) and log malformed keys by **reason
  only** — never the full PII-bearing key.
- Recommended alarms (create as needed, kept minimal here): DLQ depth > 0, Lambda
  error rate, and a `Malformed` spike.

## Idempotency caveat (important)

Count/byte rollups are idempotent **only within the dedupe-TTL window**. Once an
`OBJ#` record expires, a re-delivery, manual event replay, or a backfill of the
same key will **double-count**. Therefore:

- Keep the TTL (`TTL_DAYS`, default 90) longer than any realistic replay horizon.
- Treat replaying production events as unsafe.
- The deferred backfill (Phase 2) must be a **full rebuild** (delete-then-
  repopulate) or run through the same per-object dedupe gate — never an additive
  merge over a live index.

## Tests

Run with the CDK dev venv (`pytest` + `moto` + `aws-cdk-lib`), **not** the Django
test runner — these tests need neither Postgres nor the Beiwe app:

```bash
cd cluster_management/cdk
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt "moto[dynamodb]" boto3 pytest
.venv/bin/python -m pytest lambdas/metadata_index/tests/ -q
```

`replay_event.py` replays a fixture through the handler against a moto table for
local inspection:

```bash
.venv/bin/python lambdas/metadata_index/tests/replay_event.py \
    lambdas/metadata_index/tests/fixtures/mixed_batch.json
```
