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
| Counts / bytes over time (per participant + stream) | `STUDY#<study>#P#<patient>#S#<stream>` / `DAY#<YYYY-MM-DD>` |
| Counts / bytes over time (per stream, whole study) | `STUDY#<study>#S#<stream>` / `DAY#<YYYY-MM-DD>` |
| Study-level volume trend | `STUDY#<study>` / `DAY#<YYYY-MM-DD>` |
| Stale-stream detection | query `STUDY#<study>`, `begins_with LATEST#P#`, filter `last_upload_time < threshold` |
| Idempotency / per-object log | `OBJ#<key>` / `OBJ` (TTL'd) |

> Stale-stream detection finds streams that *were* active and stopped. Detecting
> streams **expected but never uploaded** needs Beiwe's expected-stream config and
> is deferred (Phase 2). Grafana dashboards are a downstream consumer (Phase 2);
> this layer only collects the data and exposes a `begins_with`-friendly schema.

### Study-level per-stream rollup (`STUDY#<study>#S#<stream>` / `DAY#`)

The writer keeps a study-level per-stream daily rollup **in addition to** the
participant-scoped one. It exists so per-stream study totals/trends are a bounded
read — one `Query` per stream (≤ ~20), independent of participant count — for the
in-app per-study dashboard, instead of a per-(participant,stream) fan-out. It is
always written (independent of `WRITE_STUDY_ROLLUP`) and lives in its own
partition, so it adds no load to the `STUDY#<study>` partition. It is a study-level
**aggregate**: like the `STUDY#<study>` / `DAY#` rollup it cannot single out a
participant (see participant-erasure note below). The scan-based `show_metadata_index.py`
deliberately ignores these items (it already derives per-stream totals from the
participant-scoped rollups).

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

## Stack outputs

Resolve the physical names CloudFormation generated (the commands below reference these):

```bash
aws cloudformation describe-stacks --stack-name MetadataIndexStack \
  --query "Stacks[0].Outputs" --output table
# Keys: TableName, TableArn, ReaderRoleArn, QueueUrl, DlqUrl,
#       WriterFunctionName, EventBridgeRuleName
```

## Smoke check (post-deploy)

1. Copy a tiny object to a raw-shaped, throwaway prefix:
   `aws s3 cp /tmp/x.zst s3://<bucket>/<study24>/zzsmoke01/gps/<unixms>.csv.zst`
2. Confirm a message flowed through the queue and a DynamoDB item appeared:
   ```bash
   TABLE=$(aws cloudformation describe-stacks --stack-name MetadataIndexStack \
     --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" --output text)
   aws dynamodb query --table-name "$TABLE" \
     --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
     --expression-attribute-values '{":pk":{"S":"STUDY#<study24>"},":sk":{"S":"LATEST#P#zzsmoke01"}}'
   ```
3. Delete the test object. (It is a throwaway patient id, not real data.)

## Disable vs. teardown (both leave the bucket and Beiwe untouched)

- **Disable (keep the data):** disable the EventBridge rule by its physical name
  (from the `EventBridgeRuleName` output). Ingestion stops immediately; the table
  is preserved for queries.
  ```bash
  RULE=$(aws cloudformation describe-stacks --stack-name MetadataIndexStack \
    --query "Stacks[0].Outputs[?OutputKey=='EventBridgeRuleName'].OutputValue" --output text)
  aws events disable-rule --name "$RULE"   # re-enable with: aws events enable-rule --name "$RULE"
  ```
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

1. **Latest pointers** — Query `PK = STUDY#<study>`, `begins_with(SK, "LATEST#P#<patient>")`,
   then `delete-item` each returned key (this returns both `LATEST#P#<patient>` and
   `LATEST#P#<patient>#S#<stream>`):
   ```bash
   aws dynamodb query --table-name "$TABLE" \
     --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
     --expression-attribute-values '{":pk":{"S":"STUDY#<study>"},":sk":{"S":"LATEST#P#<patient>"}}' \
     --query "Items[].{PK:PK,SK:SK}" | \
   jq -c '.[]' | while read k; do aws dynamodb delete-item --table-name "$TABLE" --key "$k"; done
   ```
2. **Per-stream daily rollups** — the PK encodes the patient, so for each stream the
   participant used, delete `PK = STUDY#<study>#P#<patient>#S#<stream>` rows (Query that
   PK, delete each `DAY#` item).
3. **`OBJ#` dedupe records** — `patient` is a non-key attribute, so this requires a
   **Scan with a FilterExpression** (`FilterExpression: patient = :p`), which needs the
   admin/deploy credentials, **not** the read-only `MetadataIndexReaderRole`. Scan and
   `delete-item` each match.
4. Confirm none remain. Note that **study-level** aggregates — both `STUDY#<study>` /
   `DAY#` and `STUDY#<study>#S#<stream>` / `DAY#` (the per-stream rollup) — do not
   single out a participant; rebuild them from raw S3 if exact study totals must
   exclude the erased participant.

> A `study_id` + `patient_id` erasure helper script (doing all three steps) is a
> reasonable future addition so the procedure is equally runnable by a human, an agent,
> or a CI job — deferred with the rest of the Phase 2 operational tooling.

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

## Schema-change reset (ordered drain)

When a writer change adds or alters a rollup shape (e.g. the study-level per-stream
rollup) and the existing index data is disposable, repopulate cleanly with an
**ordered drain** rather than a backfill — the drain prevents the reset from itself
double-counting in-flight or dead-lettered events (which would otherwise re-pass the
`attribute_not_exists` dedupe gate against a freshly-wiped table):

1. **Stop ingestion:** `aws events disable-rule --name "$RULE"` (the `EventBridgeRuleName` output).
2. **Drain the buffers:** wait for the main queue and DLQ to empty, or purge them —
   `aws sqs purge-queue --queue-url "$QueueUrl"` and the same for `$DlqUrl`. This is
   the load-bearing step: any message still queued/retryable when the table is wiped
   would re-count.
3. **Wipe:** delete all items (or delete + recreate the table).
4. **Redeploy** the writer with the new rollup: `cdk deploy MetadataIndexStack -c enable_metadata_index=true`.
5. **Resume ingestion:** `aws events enable-rule --name "$RULE"`.

The index then repopulates from go-forward uploads (no historical backfill). Only run
this against a **dev/disposable** table — confirm no consumer depends on the existing
data first.

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
