"""Idempotent DynamoDB writes for the Upload Metadata Index (U4).

Per object, two phases:

  1. claim + count : a conditional PutItem of the per-object dedupe marker
                     (attribute_not_exists) claims the object, then the count/byte
                     rollup ADDs are applied. If a rollup ADD fails, the already-
                     applied rollups are compensated (negative ADD) and the marker
                     is deleted, so the SQS retry re-claims and re-counts cleanly.
                     This guarantees that a transient mid-write failure cannot
                     leave the marker planted without its rollups (the under-count
                     bug) or leave the per-stream and study rollups disagreeing
                     (the split-brain bug). A duplicate (marker already present)
                     surfaces as ConditionalCheckFailed and skips the count.
  2. latest-advance : the participant and participant+stream LATEST# pointers
                      advance only if the new upload_time is newer (with a
                      deterministic key tie-break). These are idempotent, so they
                      run on every delivery -- including duplicates and out-of-order.

Why a compensating saga rather than TransactWriteItems: a single transaction is
the textbook fix, but the test harness (moto) cannot execute transact_write_items
under this Python, so an untestable transaction path is worse than a tested saga.
The saga closes the realistic failure (a caught transient ClientError on a rollup).
The only residual window is an uncaught process death (e.g. Lambda timeout)
BETWEEN a rollup failing and the compensating cleanup -- bounded, and recoverable
because the index is rebuildable from S3 (R4).

IMPORTANT: rollups are still only idempotent WITHIN the dedupe-TTL window. Once an
OBJ# marker expires, a re-delivery/replay/backfill of the same key re-passes the
gate and double-counts -- see the plan's idempotency caveat. Keep the TTL beyond
any realistic replay horizon and keep backfill a full rebuild or dedupe-gated.
"""
from __future__ import annotations

import os

from botocore.exceptions import ClientError

# Study-level daily rollup is the plan's "conditional" item. Default on (atomic
# ADD); if a single study's peak write rate threatens the ~1000 WCU/s partition
# ceiling (the STUDY#<study> partition also carries every participant's latest-
# pointers), set the WRITE_STUDY_ROLLUP env var to a falsy value and derive study
# totals by query-time aggregation over the per-stream rollups instead.
WRITE_STUDY_ROLLUP = str(os.environ.get("WRITE_STUDY_ROLLUP", "true")).strip().lower() in (
    "1", "true", "yes", "on",
)

WRITTEN = "written"
DUPLICATE = "duplicate"


def _obj_pk(key: str) -> str:
    return f"OBJ#{key}"


def _study_pk(study: str) -> str:
    return f"STUDY#{study}"


def _stream_rollup_pk(study: str, patient: str, stream: str) -> str:
    return f"STUDY#{study}#P#{patient}#S#{stream}"


def _study_stream_rollup_pk(study: str, stream: str) -> str:
    """Study-level per-stream daily rollup (no patient segment). This is the
    bounded-read source for the in-app per-study dashboard: per-stream study
    totals/trends are one Query per stream, instead of a per-(participant,stream)
    fan-out. Always written (independent of WRITE_STUDY_ROLLUP) and lives in its
    own partition, so it does not add load to the STUDY#<study> partition."""
    return f"STUDY#{study}#S#{stream}"


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _marker_item(record, now_epoch: int, ttl_seconds: int) -> dict:
    """The per-object dedupe marker (resource API; native Python types)."""
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
    return item


def _add_rollup(table, pk: str, day: str, count_delta: int, size_delta: int) -> None:
    """ADD count/bytes deltas to a DAY# rollup (negative deltas compensate)."""
    table.update_item(
        Key={"PK": pk, "SK": f"DAY#{day}"},
        UpdateExpression="ADD #c :c, #b :b",
        ExpressionAttributeNames={"#c": "count", "#b": "bytes"},
        ExpressionAttributeValues={":c": count_delta, ":b": size_delta},
    )


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


def _claim_and_count(table, record, now_epoch: int, ttl_seconds: int) -> bool:
    """Claim the object (conditional marker put) and apply the rollups. Returns
    True on a first sighting (claim succeeded, rollups applied), False on a
    duplicate. Raises ClientError for transient/non-conditional failures so the
    caller can retry; before raising on a rollup failure, it compensates any
    already-applied rollups and deletes the marker so the retry re-counts cleanly
    (no under-count, no per-stream/study split-brain)."""
    day = record.upload_time[:10]  # YYYY-MM-DD from the ISO event time

    # 1. Claim. ConditionalCheckFailed => already counted by a prior delivery.
    try:
        table.put_item(
            Item=_marker_item(record, now_epoch, ttl_seconds),
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise

    # 2. Count. Compensate + un-claim on failure so the retry re-counts exactly once.
    # Order matters: the participant-scoped rollup stays first so the
    # compensate-and-unclaim saga (and its call-count-based tests) are stable.
    # The study-level per-stream rollup is always written; the study-level total
    # rollup is gated by WRITE_STUDY_ROLLUP.
    rollup_pks = [
        _stream_rollup_pk(record.study, record.patient, record.stream),
        _study_stream_rollup_pk(record.study, record.stream),
    ]
    if WRITE_STUDY_ROLLUP:
        rollup_pks.append(_study_pk(record.study))

    applied = []
    try:
        for pk in rollup_pks:
            _add_rollup(table, pk, day, 1, record.size)
            applied.append(pk)
    except ClientError:
        for pk in applied:  # best-effort compensation of the partial counts
            try:
                _add_rollup(table, pk, day, -1, -record.size)
            except ClientError:
                pass
        try:
            table.delete_item(Key={"PK": _obj_pk(record.key), "SK": "OBJ"})
        except ClientError:
            pass
        raise
    return True


def write_record(table, record, now_epoch: int, ttl_seconds: int) -> str:
    """Write one parsed upload record idempotently. Returns WRITTEN or DUPLICATE.

    Raises ClientError for transient/non-conditional failures so the caller can
    return the message to SQS for retry.
    """
    first_time = _claim_and_count(table, record, now_epoch, ttl_seconds)

    # Latest pointers advance on every delivery (idempotent advance-only), so a
    # duplicate or out-of-order event never regresses or wrongly advances them.
    _advance_latest_pointer(
        table, _study_pk(record.study), f"LATEST#P#{record.patient}",
        record, include_stream=True,
    )
    _advance_latest_pointer(
        table, _study_pk(record.study), f"LATEST#P#{record.patient}#S#{record.stream}",
        record, include_stream=False,
    )

    return WRITTEN if first_time else DUPLICATE
