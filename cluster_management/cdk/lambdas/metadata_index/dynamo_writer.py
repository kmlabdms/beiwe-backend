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
import zlib

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

# Number of partitions the per-participant daily aggregate is sharded across (the
# bounded-read source for the adherence heatmap / sparklines). LOAD-BEARING: the
# reader (libs/metadata_index_reader.py, vendored copy) and the backfill must use
# the SAME value and the SAME _shard_for; changing it requires a coordinated deploy
# + re-backfill. The value is persisted to a CONFIG item so the reader can assert
# it matches and fail loudly on drift rather than silently dropping participants.
SHARDS = 8

# Ensure-once-per-container guard for the CONFIG/SHARDS item (avoids a write per upload).
_config_ensured = False


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


def _shard_for(patient: str) -> int:
    """Deterministic shard index for a patient. MUST be byte-for-byte identical in
    the reader and backfill -- never use the builtin hash() (randomized per process)."""
    return zlib.crc32(patient.encode()) % SHARDS


def _participant_daily_pk(study: str, patient: str) -> str:
    """Sharded per-participant daily aggregate partition. SK is P#<patient>#DAY#<day>,
    so one Query per shard (begins_with P#) reassembles the whole participant x day
    adherence matrix -- bounded by SHARDS, independent of participant count."""
    return f"STUDY#{study}#DAILY#{_shard_for(patient)}"


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


def _add_rollup(table, pk: str, sk: str, count_delta: int, size_delta: int) -> None:
    """ADD count/bytes deltas to a rollup item at (pk, sk) (negative deltas compensate).
    The SK is passed explicitly because rollup targets no longer share one SK shape:
    the count rollups use DAY#<day> while the participant-daily aggregate uses
    P#<patient>#DAY#<day>."""
    table.update_item(
        Key={"PK": pk, "SK": sk},
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


def _advance_first_pointer(table, pk: str, sk: str, day: str) -> None:
    """Keep the EARLIEST upload day ever seen for a participant (the mirror of
    _advance_latest_pointer). Day-granular on purpose: the per-stream rollups carry
    no timestamp, so the backfill can only derive the minimum DAY#, and writer +
    backfill must agree on the same field. ISO-date string compare == chronological."""
    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="SET first_day = :d",
            ConditionExpression="attribute_not_exists(first_day) OR first_day > :d",
            ExpressionAttributeValues={":d": day},
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return  # stored first_day already earlier/equal -- expected, not an error
        raise


def _ensure_config(table) -> None:
    """Persist the running writer's SHARDS so the reader can assert against it and
    fail loudly on drift. Bounded to ~once per Lambda container via a module flag
    (not once per upload). Best-effort: the reader tolerates an absent CONFIG."""
    global _config_ensured
    if _config_ensured:
        return
    try:
        table.put_item(Item={"PK": "CONFIG", "SK": "SHARDS", "value": SHARDS})
    except ClientError:
        pass
    _config_ensured = True


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
    # Targets are (pk, sk) pairs because they no longer share one SK shape. Order
    # matters: the participant-scoped rollup stays FIRST and the participant-daily
    # aggregate is appended LAST, so the existing call-count-based saga tests keep
    # their targets (call #1 = per-stream, #2 = study-stream). The study-level total
    # rollup is gated by WRITE_STUDY_ROLLUP; the per-stream and participant-daily
    # rollups are always written.
    day_sk = f"DAY#{day}"
    targets = [
        (_stream_rollup_pk(record.study, record.patient, record.stream), day_sk),
        (_study_stream_rollup_pk(record.study, record.stream), day_sk),
    ]
    if WRITE_STUDY_ROLLUP:
        targets.append((_study_pk(record.study), day_sk))
    targets.append(
        (_participant_daily_pk(record.study, record.patient), f"P#{record.patient}#{day_sk}")
    )

    applied = []
    try:
        for pk, sk in targets:
            _add_rollup(table, pk, sk, 1, record.size)
            applied.append((pk, sk))
    except ClientError:
        for pk, sk in applied:  # best-effort compensation of the partial counts
            try:
                _add_rollup(table, pk, sk, -1, -record.size)
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
    _ensure_config(table)
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
    # First-seen day pointer advances earliest (idempotent), like the latest pointers.
    _advance_first_pointer(
        table, _study_pk(record.study), f"FIRST#P#{record.patient}", record.upload_time[:10],
    )

    return WRITTEN if first_time else DUPLICATE
