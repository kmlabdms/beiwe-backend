"""moto-backed tests for the writer Lambda (U4).

Exercises the full SQS -> parse -> idempotent DynamoDB write path against a real
(mocked) DynamoDB table: happy batch, idempotency, the documented TTL-expiry
double-count boundary, out-of-order latest-pointers, envelope unwrapping, and
the malformed/ignored/corrupt/transient branches.
"""
import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import dynamo_writer
import handler

STUDY = "2grtzwKjSxi64uYkxqZASgxe"
PATIENT = "7xhpe54h"
TABLE_NAME = "test-upload-metadata-index"
DAY = "2026-05-28"


def _eb_message(key, size, message_id, time_=f"{DAY}T20:27:34Z", bucket="raw-bucket"):
    body = {
        "detail-type": "Object Created",
        "source": "aws.s3",
        "time": time_,
        "detail": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}},
    }
    return {"messageId": message_id, "body": json.dumps(body)}


def _sqs_event(*messages):
    return {"Records": list(messages)}


def _raw_key(stream, ts):
    return f"{STUDY}/{PATIENT}/{stream}/{ts}.csv.zst"


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        tbl = resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        tbl.wait_until_exists()
        os.environ["TABLE_NAME"] = TABLE_NAME
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        handler._TABLE = None  # force lazy rebuild inside the mock
        yield tbl
        handler._TABLE = None
        os.environ.pop("TABLE_NAME", None)


def _item(table, pk, sk):
    return table.get_item(Key={"PK": pk, "SK": sk}).get("Item")


def _stream_rollup(table, stream):
    return _item(table, f"STUDY#{STUDY}#P#{PATIENT}#S#{stream}", f"DAY#{DAY}")


def _study_stream_rollup(table, stream):
    """Study-level per-stream daily rollup (no patient segment) -- the dashboard's
    bounded-read source."""
    return _item(table, f"STUDY#{STUDY}#S#{stream}", f"DAY#{DAY}")


# --- happy path --------------------------------------------------------------

def test_happy_batch_writes_dedupe_latest_and_rollups(table):
    event = _sqs_event(
        _eb_message(_raw_key("accel", 1779996316436), 100, "m1"),
        _eb_message(_raw_key("accel", 1779996320000), 200, "m2"),
        _eb_message(_raw_key("gps", 1779996330000), 50, "m3"),
    )
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == []

    # per-stream rollups: accelerometer saw 2 objects/300 bytes, gps 1/50
    accel = _stream_rollup(table, "accelerometer")
    assert int(accel["count"]) == 2 and int(accel["bytes"]) == 300
    gps = _stream_rollup(table, "gps")
    assert int(gps["count"]) == 1 and int(gps["bytes"]) == 50

    # study-level rollup aggregates all three
    study = _item(table, f"STUDY#{STUDY}", f"DAY#{DAY}")
    assert int(study["count"]) == 3 and int(study["bytes"]) == 350

    # study-level per-stream rollups (the dashboard's bounded-read source): same
    # per-stream totals as the participant-scoped rollups, but without a patient
    # segment so they're readable in one Query per stream.
    accel_ss = _study_stream_rollup(table, "accelerometer")
    assert int(accel_ss["count"]) == 2 and int(accel_ss["bytes"]) == 300
    gps_ss = _study_stream_rollup(table, "gps")
    assert int(gps_ss["count"]) == 1 and int(gps_ss["bytes"]) == 50

    # participant + participant/stream latest pointers exist
    assert _item(table, f"STUDY#{STUDY}", f"LATEST#P#{PATIENT}") is not None
    assert _item(table, f"STUDY#{STUDY}", f"LATEST#P#{PATIENT}#S#accelerometer") is not None


# --- idempotency -------------------------------------------------------------

def test_duplicate_delivery_counts_once(table):
    msg = _eb_message(_raw_key("gps", 1779996330000), 50, "dup")
    handler.handler(_sqs_event(msg), None)
    handler.handler(_sqs_event(dict(msg, messageId="dup-again")), None)  # same key, redelivered
    gps = _stream_rollup(table, "gps")
    assert int(gps["count"]) == 1 and int(gps["bytes"]) == 50  # not double-counted


def test_ttl_expiry_re_delivery_double_counts(table):
    """Documents the idempotency boundary: once the dedupe marker is gone (TTL
    expiry), the same object re-counts. This is why the TTL must exceed any
    realistic replay horizon and backfill must be a full rebuild."""
    key = _raw_key("gps", 1779996330000)
    handler.handler(_sqs_event(_eb_message(key, 50, "first")), None)
    # simulate TTL expiry: delete the per-object dedupe marker
    table.delete_item(Key={"PK": f"OBJ#{key}", "SK": "OBJ"})
    handler.handler(_sqs_event(_eb_message(key, 50, "after-expiry")), None)
    gps = _stream_rollup(table, "gps")
    assert int(gps["count"]) == 2  # re-counted -- the documented exposure


# --- latest-pointer ordering -------------------------------------------------

def test_out_of_order_does_not_regress_latest_pointer(table):
    newer = _eb_message(_raw_key("accel", 1779996320000), 10, "n", time_=f"{DAY}T21:00:00Z")
    older = _eb_message(_raw_key("accel", 1779996316436), 10, "o", time_=f"{DAY}T20:00:00Z")
    handler.handler(_sqs_event(newer), None)
    handler.handler(_sqs_event(older), None)  # arrives later but is older
    latest = _item(table, f"STUDY#{STUDY}", f"LATEST#P#{PATIENT}#S#accelerometer")
    assert latest["last_upload_time"] == f"{DAY}T21:00:00Z"  # newer one retained


def test_equal_time_tie_break_is_deterministic(table):
    # same upload_time, two different keys -- the larger key must win regardless
    # of arrival order.
    key_a = _raw_key("accel", 1779996316436)
    key_b = _raw_key("accel", 1779996399999)  # lexically larger
    t = f"{DAY}T20:00:00Z"
    handler.handler(_sqs_event(_eb_message(key_b, 10, "b", time_=t)), None)
    handler.handler(_sqs_event(_eb_message(key_a, 10, "a", time_=t)), None)  # smaller, later
    latest = _item(table, f"STUDY#{STUDY}", f"LATEST#P#{PATIENT}#S#accelerometer")
    assert latest["last_key"] == key_b  # tie broken toward the larger key, order-independent


# --- envelope shapes ---------------------------------------------------------

def test_s3_notification_envelope_unwrapped(table):
    key = _raw_key("gps", 1779996330000)
    body = {"Records": [{
        "eventTime": f"{DAY}T20:27:34Z",
        "s3": {"bucket": {"name": "raw-bucket"}, "object": {"key": key, "size": 77}},
    }]}
    event = _sqs_event({"messageId": "s3msg", "body": json.dumps(body)})
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == []
    assert int(_stream_rollup(table, "gps")["bytes"]) == 77


def test_s3_test_event_is_skipped(table):
    body = {"Event": "s3:TestEvent", "Service": "Amazon S3"}  # no Records/detail
    result = handler.handler(_sqs_event({"messageId": "t", "body": json.dumps(body)}), None)
    assert result["batchItemFailures"] == []  # nothing to do, no crash


# --- malformed / ignored / corrupt / transient ------------------------------

def test_malformed_key_does_not_fail_batch(table):
    bad = _eb_message(f"{STUDY}/{PATIENT}/notastream/123.csv.zst", 10, "bad")
    result = handler.handler(_sqs_event(bad), None)
    assert result["batchItemFailures"] == []  # logged + metered, never retried
    assert _stream_rollup(table, "notastream") is None


def test_ignored_prefix_writes_nothing(table):
    ignored = _eb_message(f"CHUNKED_DATA/{STUDY}/{PATIENT}/gps/x.csv.zst", 10, "ig")
    result = handler.handler(_sqs_event(ignored), None)
    assert result["batchItemFailures"] == []
    assert _item(table, f"STUDY#{STUDY}", f"DAY#{DAY}") is None  # no rollup at all


def test_corrupt_body_reported_as_batch_failure(table):
    event = _sqs_event({"messageId": "corrupt", "body": "this is not json{"})
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "corrupt"}]


def test_transient_dynamo_error_is_retried(table, monkeypatch):
    def boom(*_args, **_kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    monkeypatch.setattr(dynamo_writer, "write_record", boom)
    event = _sqs_event(_eb_message(_raw_key("gps", 1779996330000), 10, "throttled"))
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "throttled"}]


def test_mixed_batch(table):
    event = _sqs_event(
        _eb_message(_raw_key("accel", 1779996316436), 10, "valid"),
        _eb_message(f"CHUNKED_DATA/{STUDY}/{PATIENT}/gps/x.csv.zst", 10, "ignore"),
        _eb_message(f"{STUDY}/{PATIENT}/notastream/1.csv.zst", 10, "malformed"),
        _eb_message(_raw_key("accel", 1779996316436), 10, "duplicate"),  # same as "valid"
    )
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == []
    accel = _stream_rollup(table, "accelerometer")
    assert int(accel["count"]) == 1  # valid counted once; duplicate not re-counted


# --- code-review fixes: saga rollback, batch isolation, metrics, toggles -----

def test_transient_rollup_failure_rolls_back_and_retry_recounts_once(table, monkeypatch):
    """The cited P1: a transient failure between the dedupe marker and the rollups
    must NOT leave a marker that suppresses the count on retry. The saga deletes
    the marker so the retry re-counts exactly once."""
    real_add = dynamo_writer._add_rollup
    calls = {"n": 0}

    def flaky_add(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # fail the very first rollup ADD
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(dynamo_writer, "_add_rollup", flaky_add)
    key = _raw_key("gps", 1779996330000)
    r1 = handler.handler(_sqs_event(_eb_message(key, 50, "m1")), None)
    assert r1["batchItemFailures"] == [{"itemIdentifier": "m1"}]   # returned for retry
    assert _item(table, f"OBJ#{key}", "OBJ") is None               # marker rolled back
    assert _stream_rollup(table, "gps") is None                    # no rollup applied

    r2 = handler.handler(_sqs_event(_eb_message(key, 50, "m1")), None)  # SQS retry
    assert r2["batchItemFailures"] == []
    assert int(_stream_rollup(table, "gps")["count"]) == 1         # counted exactly once


def test_split_brain_avoided_when_second_rollup_fails(table, monkeypatch):
    """If the second rollup (now the study-level per-stream rollup) fails after the
    participant-scoped rollup applied, the applied one is compensated and the marker
    removed, so the retry leaves all three rollups consistent (each == 1), never
    split-brained."""
    real_add = dynamo_writer._add_rollup
    calls = {"n": 0}

    def flaky_add(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # fail the SECOND rollup ADD on first delivery
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(dynamo_writer, "_add_rollup", flaky_add)
    key = _raw_key("gps", 1779996330000)
    handler.handler(_sqs_event(_eb_message(key, 50, "m1")), None)   # fails + compensates
    handler.handler(_sqs_event(_eb_message(key, 50, "m1")), None)   # retry succeeds
    stream = _stream_rollup(table, "gps")
    study_stream = _study_stream_rollup(table, "gps")
    study = _item(table, f"STUDY#{STUDY}", f"DAY#{DAY}")
    assert int(stream["count"]) == 1 and int(stream["bytes"]) == 50
    assert int(study_stream["count"]) == 1 and int(study_stream["bytes"]) == 50  # consistent
    assert int(study["count"]) == 1 and int(study["bytes"]) == 50   # consistent, not 2-vs-1


def test_non_clienterror_isolated_to_its_message(table, monkeypatch):
    real_parse = handler.parser.parse

    def maybe_boom(**kwargs):
        if "boom" in kwargs["key"]:
            raise ValueError("kaboom")  # a non-ClientError
        return real_parse(**kwargs)

    monkeypatch.setattr(handler.parser, "parse", maybe_boom)
    event = _sqs_event(
        _eb_message(_raw_key("gps", 1779996330000), 50, "good"),
        _eb_message(f"{STUDY}/{PATIENT}/gps/boom.csv.zst", 10, "poison"),
    )
    result = handler.handler(event, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "poison"}]  # only the poison
    assert int(_stream_rollup(table, "gps")["count"]) == 1                # healthy one written


def test_missing_object_key_does_not_write_or_fail(table):
    body = {  # EventBridge body whose object has no "key"
        "detail-type": "Object Created", "source": "aws.s3", "time": f"{DAY}T20:00:00Z",
        "detail": {"bucket": {"name": "b"}, "object": {"size": 10}},
    }
    result = handler.handler(_sqs_event({"messageId": "nokey", "body": json.dumps(body)}), None)
    assert result["batchItemFailures"] == []
    assert _item(table, f"STUDY#{STUDY}", f"DAY#{DAY}") is None


def test_missing_event_time_is_malformed_no_writes(table):
    body = {  # EventBridge body with NO "time" field
        "detail-type": "Object Created", "source": "aws.s3",
        "detail": {"bucket": {"name": "b"}, "object": {"key": _raw_key("gps", 1779996330000), "size": 10}},
    }
    result = handler.handler(_sqs_event({"messageId": "nt", "body": json.dumps(body)}), None)
    assert result["batchItemFailures"] == []          # malformed, not retried
    assert _stream_rollup(table, "gps") is None        # nothing written


def test_metrics_emitted_when_namespace_set(table, monkeypatch):
    captured = []

    class FakeCloudWatch:
        def put_metric_data(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setenv("METRIC_NAMESPACE", "BeiweUploadMetadata")
    monkeypatch.setattr(handler, "_CLOUDWATCH", FakeCloudWatch())
    handler.handler(_sqs_event(_eb_message(_raw_key("gps", 1779996330000), 10, "m")), None)
    assert captured and captured[0]["Namespace"] == "BeiweUploadMetadata"
    assert "Written" in {m["MetricName"] for m in captured[0]["MetricData"]}


def test_metric_emission_failure_does_not_break_processing(table, monkeypatch):
    class BoomCloudWatch:
        def put_metric_data(self, **kwargs):
            raise RuntimeError("cloudwatch down")

    monkeypatch.setenv("METRIC_NAMESPACE", "BeiweUploadMetadata")
    monkeypatch.setattr(handler, "_CLOUDWATCH", BoomCloudWatch())
    result = handler.handler(_sqs_event(_eb_message(_raw_key("gps", 1779996330000), 10, "m")), None)
    assert result["batchItemFailures"] == []                       # metrics are best-effort
    assert int(_stream_rollup(table, "gps")["count"]) == 1         # write still happened


def test_study_rollup_can_be_disabled(table, monkeypatch):
    monkeypatch.setattr(dynamo_writer, "WRITE_STUDY_ROLLUP", False)
    handler.handler(_sqs_event(_eb_message(_raw_key("gps", 1779996330000), 10, "m")), None)
    assert _stream_rollup(table, "gps") is not None                # per-stream still written
    assert _item(table, f"STUDY#{STUDY}", f"DAY#{DAY}") is None     # study rollup suppressed
