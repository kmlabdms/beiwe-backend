"""Metadata-index writer Lambda entrypoint (U4).

Consumes an SQS batch (each message wraps either an EventBridge "Object Created"
event or an S3 notification), parses key + event metadata only, and performs
idempotent DynamoDB writes. Uses SQS partial-batch-failure reporting so only
genuinely-failed messages are retried (and eventually dead-lettered).

The function never calls S3 -- object size and time come from the event record,
making "we never read raw contents" (R2) an IAM-enforced guarantee.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

import dynamo_writer
import parser

log = logging.getLogger()
log.setLevel(logging.INFO)

_TABLE = None  # lazy singleton so moto's mock is active before the client is built


def _get_table():
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _TABLE


def _ttl_seconds() -> int:
    return int(os.environ.get("TTL_DAYS", "90")) * 86400


def _extract_events(body: dict):
    """Yield (bucket, key, size, upload_time) tuples from one SQS message body,
    handling both the EventBridge and S3-notification envelope shapes."""
    detail = body.get("detail")
    if isinstance(detail, dict) and "object" in detail:  # EventBridge "Object Created"
        obj = detail["object"]
        yield (
            detail.get("bucket", {}).get("name"),
            obj.get("key"),
            obj.get("size", 0),
            body.get("time"),
        )
        return
    for record in body.get("Records", []):  # S3 notification (skips s3:TestEvent)
        s3 = record.get("s3")
        if not s3:
            continue
        yield (
            s3.get("bucket", {}).get("name"),
            s3.get("object", {}).get("key"),
            s3.get("object", {}).get("size", 0),
            record.get("eventTime"),
        )


def _emit_metrics(counts: dict) -> None:
    """Best-effort CloudWatch metrics. No-op unless METRIC_NAMESPACE is set, and
    never fails the invocation."""
    namespace = os.environ.get("METRIC_NAMESPACE")
    if not namespace or not counts:
        return
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace=namespace,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}
                        for name, value in counts.items()],
        )
    except Exception as exc:  # metrics are observability, not correctness
        log.warning("metric emission failed: %s", exc)


def handler(event, context):
    table = _get_table()
    now = int(time.time())
    ttl = _ttl_seconds()
    counts: dict = defaultdict(int)
    failures = []

    for message in event.get("Records", []):
        message_id = message.get("messageId")
        try:
            body = json.loads(message["body"])
        except (ValueError, KeyError, TypeError):
            # A body we can't even parse will never succeed -- return it for
            # retry so SQS eventually dead-letters it rather than crashing here.
            counts["CorruptBody"] += 1
            if message_id:
                failures.append(message_id)
            continue

        try:
            for bucket, key, size, upload_time in _extract_events(body):
                if not key:
                    counts["Malformed"] += 1
                    continue
                outcome = parser.parse(
                    key=key, size=size or 0, upload_time=upload_time or "", bucket=bucket,
                )
                if isinstance(outcome, parser.MetadataRecord):
                    result = dynamo_writer.write_record(table, outcome, now, ttl)
                    counts["Duplicate" if result == dynamo_writer.DUPLICATE else "Written"] += 1
                elif isinstance(outcome, parser.Ignore):
                    counts["Ignored"] += 1
                else:  # Malformed -- log the reason only, never the PII-bearing key
                    counts["Malformed"] += 1
                    log.warning("malformed upload key skipped: reason=%s", outcome.reason)
        except ClientError as exc:
            # Transient/dependency failure (e.g. throttling). Return the whole
            # message for retry; the writes already applied are idempotent.
            counts["RetryableError"] += 1
            log.warning("retryable write failure: %s", exc.response.get("Error", {}).get("Code"))
            if message_id:
                failures.append(message_id)

    _emit_metrics(counts)
    log.info("batch processed: %s", dict(counts))
    return {"batchItemFailures": [{"itemIdentifier": mid} for mid in failures]}
