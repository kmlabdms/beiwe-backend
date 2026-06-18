"""Idempotent DynamoDB writes for the Upload Metadata Index (U4).

Write order per object (each step independently safe to retry):

  1. dedupe-gate    : conditional PutItem of OBJ#<key> (attribute_not_exists(PK)).
                      Succeeds once per object; on the duplicate path it raises
                      ConditionalCheckFailed and we skip the rollups.
  2. latest-advance : UpdateItem of the participant and participant+stream
                      LATEST# pointers, advancing ONLY if the new upload_time is
                      newer (with a deterministic key tie-break on equal times).
                      Idempotent -- safe to run on duplicates and out-of-order.
  3. rollup-add     : atomic ADD to per-stream and study daily counters, gated on
                      the dedupe-gate succeeding so counts stay exact.

IMPORTANT: rollups are only idempotent WITHIN the dedupe-TTL window. Once an
OBJ# item expires, a re-delivery/replay/backfill of the same key re-passes the
gate and double-counts -- see the plan's idempotency caveat. Set the TTL beyond
any realistic replay horizon and keep backfill a full rebuild or dedupe-gated.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

# Study-level daily rollup is the plan's "conditional" item: implemented as an
# atomic ADD initially. If a single study's peak write rate threatens the
# ~1000 WCU/s partition ceiling (the STUDY#<study> partition also carries every
# participant's latest-pointers), switch to deriving study totals by query-time
# aggregation over the per-stream rollups and flip this to False.
WRITE_STUDY_ROLLUP = True

WRITTEN = "written"
DUPLICATE = "duplicate"


def _obj_pk(key: str) -> str:
    return f"OBJ#{key}"


def _study_pk(study: str) -> str:
    return f"STUDY#{study}"


def _stream_rollup_pk(study: str, patient: str, stream: str) -> str:
    return f"STUDY#{study}#P#{patient}#S#{stream}"


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _put_dedupe_marker(table, record, now_epoch: int, ttl_seconds: int) -> bool:
    """Return True if this is the first time we've seen the object (gate passed),
    False if it's a duplicate. Re-raises any non-conditional error for retry."""
    item = {
        "PK": _obj_pk(record.key),
        "SK": "OBJ",
        "study": record.study,
        "patient": record.patient,
        "stream": record.stream,
        "upload_time": record.upload_time,
        "size": record.size,
        "ttl": now_epoch + ttl_seconds,
    }
    if record.device_time is not None:
        item["device_time"] = record.device_time
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def _advance_latest_pointer(table, pk: str, sk: str, record, include_stream: bool) -> None:
    """Advance a LATEST# pointer iff the new upload_time is newer, breaking exact
    ties deterministically by key so the stored pointer is arrival-order
    independent. A no-op (swallowed conditional failure) when the stored value is
    already newer/equal-with-larger-key."""
    set_parts = ["last_upload_time = :t", "last_key = :k", "last_size = :s"]
    values = {":t": record.upload_time, ":k": record.key, ":s": record.size}
    if include_stream:
        set_parts.append("last_stream = :st")
        values[":st"] = record.stream
    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=(
                "attribute_not_exists(last_upload_time) "
                "OR last_upload_time < :t "
                "OR (last_upload_time = :t AND last_key < :k)"
            ),
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return  # stored pointer already newer -- expected, not an error
        raise


def _add_rollup(table, pk: str, day: str, size: int) -> None:
    table.update_item(
        Key={"PK": pk, "SK": f"DAY#{day}"},
        UpdateExpression="ADD #c :one, #b :sz",
        ExpressionAttributeNames={"#c": "count", "#b": "bytes"},
        ExpressionAttributeValues={":one": 1, ":sz": size},
    )


def write_record(table, record, now_epoch: int, ttl_seconds: int) -> str:
    """Write one parsed upload record idempotently. Returns WRITTEN or DUPLICATE.

    Raises ClientError for transient/non-conditional failures so the caller can
    return the message to SQS for retry.
    """
    first_time = _put_dedupe_marker(table, record, now_epoch, ttl_seconds)

    # Latest pointers advance on every delivery (idempotent), so a duplicate or
    # out-of-order event never regresses or wrongly advances them.
    _advance_latest_pointer(
        table, _study_pk(record.study), f"LATEST#P#{record.patient}",
        record, include_stream=True,
    )
    _advance_latest_pointer(
        table, _study_pk(record.study), f"LATEST#P#{record.patient}#S#{record.stream}",
        record, include_stream=False,
    )

    if not first_time:
        return DUPLICATE

    # Rollups only on the first sighting -- gated by the dedupe marker above.
    day = record.upload_time[:10]  # YYYY-MM-DD from the ISO event time
    _add_rollup(table, _stream_rollup_pk(record.study, record.patient, record.stream), day, record.size)
    if WRITE_STUDY_ROLLUP:
        _add_rollup(table, _study_pk(record.study), day, record.size)
    return WRITTEN
