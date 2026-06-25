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
| Participant×day adherence (sharded) | `STUDY#<study>#DAILY#<shard>` / `P#<patient>#DAY#<YYYY-MM-DD>` (query all shards, `begins_with P#`) |
| First upload day per participant (enrollment) | `STUDY#<study>` / `FIRST#P#<patient>` (`first_day`) |
| Shard count (reader asserts to catch drift) | `CONFIG` / `SHARDS` |
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
3. **Participant-daily aggregate (sharded)** — the patient is in the SK, spread across
   shards, so loop **all `SHARDS` partitions** and delete the participant's day rows:
   for `shard` in `0..SHARDS-1`, Query `PK = STUDY#<study>#DAILY#<shard>`,
   `begins_with(SK, "P#<patient>#DAY#")`, delete each. (The trailing `#DAY#` in the
   prefix is required — `begins_with(SK, "P#<patient>")` would also match a participant
   whose id starts with `<patient>`, e.g. erasing `abc` would hit `abc123`.)
4. **First-seen pointer** — delete the single exact key `PK = STUDY#<study>`,
   `SK = FIRST#P#<patient>` (`delete-item`, not a `begins_with` query, to avoid the
   same prefix-collision).
5. **`OBJ#` dedupe records** — `patient` is a non-key attribute, so this requires a
   **Scan with a FilterExpression** (`FilterExpression: patient = :p`), which needs the
   admin/deploy credentials, **not** the read-only `MetadataIndexReaderRole`. Scan and
   `delete-item` each match.
6. Confirm none remain. Note that **study-level** aggregates — `STUDY#<study>` / `DAY#`
   and `STUDY#<study>#S#<stream>` / `DAY#` (the per-stream rollup) — do not single out a
   participant; rebuild them from raw S3 if exact study totals must exclude the erased
   participant.

> **Prefix-collision caveat (applies to steps 1 and 3):** the existing `LATEST#P#<patient>`
> deletes (step 1) share this hazard — if patient ids are not fixed-length, a `begins_with`
> on a bare patient prefix can match a longer id. Append a trailing delimiter (`#` /
> `#S#` / `#DAY#`) or do exact-key deletes when ids may share prefixes.

> A `study_id` + `patient_id` erasure helper script (doing all three steps) is a
> reasonable future addition so the procedure is equally runnable by a human, an agent,
> or a CI job — deferred with the rest of the Phase 2 operational tooling.

## Reader access (the Beiwe web app, Grafana, humans)

Read the table via the least-privilege `MetadataIndexReaderRole` (the
`ReaderRoleArn` stack output): `dynamodb:Query` + `GetItem` only, no `Scan`, no
writes. Grant `sts:AssumeRole` on it to the specific reader principal — **do not**
read the table with the account-wide AdministratorAccess deploy user. The table is
effectively a full participant-upload roster.

Scope the role's **trust** to that principal at deploy time so it isn't assumable
account-wide:

```bash
cdk deploy MetadataIndexStack -c enable_metadata_index=true \
  -c reader_principal_arn=arn:aws:iam::<acct>:user/<beiwe-web-iam-user>
```

### Wiring the in-app dashboard (`endpoints/metadata_dashboard_endpoints.py`)

The Django web tier reads the index by assuming `ReaderRoleArn` with its existing
`BEIWE_SERVER_AWS_*` credentials (refreshable STS creds; no expiry on a long-lived
worker).

**Authorization is same-account.** The web tier and this stack live in the same AWS
account, and assume-role authorization can come from *either* side of the trust:

- **Recommended (scoped trust, no extra IAM):** deploy with `reader_principal_arn`
  set to the web server's IAM principal. For same-account assumption, a trust policy
  that names a *specific* principal is sufficient on its own — **no identity-based
  `sts:AssumeRole` grant on the web principal is required.** Deploying the stack is
  the whole authorization step.
- **Identity-side grant is only needed if:** you used the `AccountRootPrincipal`
  fallback (omitted `reader_principal_arn`, so the trust delegates to the account and
  the caller must hold its own `sts:AssumeRole` on the role ARN), **or** an SCP /
  permission boundary on the web principal requires an explicit allow. This grant is
  an out-of-band IAM edit on the web user/role because that principal is provisioned
  by the EB / `launch_script.py` deployment, not by this additive stack (which never
  mutates Beiwe-owned IAM). Last-resort fallback: grant the web principal
  `dynamodb:Query`/`GetItem` directly on the table ARN.

Then **app settings** — set `METADATA_INDEX_ENABLED=true`, `METADATA_INDEX_TABLE_NAME`
(the `TableName` output), `METADATA_INDEX_REGION`, and `METADATA_INDEX_READER_ROLE_ARN`
(the `ReaderRoleArn` output) in the web environment; keep `DEBUG=False`.

### Scripted deploy (`make` / `deploy_metadata_index.sh`)

`deploy_metadata_index.sh` automates the above; the repo-root `Makefile` wraps it.
Mutating targets are **preview-only** unless you pass `APPLY=true` (or `EXECUTE=true`
for the reset), so a bare run never changes anything.

```bash
make metadata-index-web-arn AWS_PROFILE=<p>     # resolve the runtime web principal ARN
make metadata-index-outputs AWS_PROFILE=<p>     # show TableName / ReaderRoleArn / region
make metadata-index-deploy  AWS_PROFILE=<p>             # PREVIEW: cdk deploy (scoped trust) + set web env
make metadata-index-deploy  AWS_PROFILE=<p> APPLY=true  # actually deploy the stack + eb setenv
make metadata-index-set-env AWS_PROFILE=<p> APPLY=true  # just (re)set the EB web env from stack outputs
make metadata-index-backfill AWS_PROFILE=<p>            # PREVIEW the participant-daily backfill
make metadata-index-backfill AWS_PROFILE=<p> APPLY=true # pause ingestion, backfill, resume
```

All resolution and mutation go through the `aws` CLI (no `eb` CLI dependency).
`deploy` auto-derives `reader_principal_arn` (the principal that assumes the reader
role) by reading the web env's `BEIWE_SERVER_AWS_ACCESS_KEY_ID` off the EB environment
and mapping it to its IAM user (or, if the web tier has no such keys, the EB
instance-profile role); override with `READER_PRINCIPAL_ARN=...`. The web env vars are
written with `aws elasticbeanstalk update-environment` against `EB_APP`/`EB_ENV`
(default `beiwe-application` / `kowalski-beiwe`, overridable). The deployer's profile
needs `elasticbeanstalk:*`, `iam:GetAccessKeyLastUsed`/`GetInstanceProfile`, and
`cloudformation:DescribeStacks`.

### Participant-daily backfill (non-destructive)

`metadata-index-backfill` populates the sharded participant-daily aggregate and the
`FIRST#` first-seen pointers from the per-(participant,stream) rollups already in the
table — **no wipe**. It is the way to fill these views with history on an existing
deployment (the writer otherwise only accrues them going forward).

`--apply` orchestrates: **disable** the EventBridge rule → **wait** for the queue +
DLQ to drain to empty (the live writer finishes queued uploads into the rollups —
this is a drain-by-**wait**, never a purge: purging a live table would drop those
uploads from *every* rollup) → run `backfill_participant_daily.py --apply`, which
`SET`s the derived items (absolute, idempotent) and **refuses to write unless the rule
is disabled and the queues are empty** → **re-enable** the rule. Safe to re-run, but
**only while ingestion is paused** (a `SET` racing the live `ADD` corrupts the value).
The backfill needs admin/deploy creds (`dynamodb:Scan`/`Query`/`UpdateItem` on the
table, `events`/`sqs`/`cloudformation` describe) — not the reader role.

The destructive schema reset is a separate, double-gated alternative (a full clean
slate; loses history). Unlike the backfill, it **purges** — correct only because it
wipes the table:

```bash
make metadata-index-reset AWS_PROFILE=<p>                                          # prints the plan
make metadata-index-reset AWS_PROFILE=<p> EXECUTE=true I_UNDERSTAND_THIS_DELETES_DATA=yes   # runs it
```

**Verify before relying on the page** (run as the web principal):

```bash
CREDS=$(aws sts assume-role --role-arn "$ReaderRoleArn" --role-session-name verify --query Credentials --output json)
AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId) \
AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey) \
AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .SessionToken) \
aws dynamodb query --table-name "$TableName" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
  --expression-attribute-values '{":pk":{"S":"STUDY#<study24>"},":sk":{"S":"DAY#"}}'
```

If the grant is missing the dashboard surfaces a loud "could not reach the index"
state (it never silently looks "disabled").

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
