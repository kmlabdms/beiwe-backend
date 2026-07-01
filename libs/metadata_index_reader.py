"""Read-only access to the Upload Metadata Index (DynamoDB) for the in-app per-study
dashboard.

This is the Phase-2 consumer of the additive collection layer
(cluster_management/cdk/METADATA_INDEX.md). It NEVER writes, NEVER scans, and NEVER
reads raw S3. Every read is a per-study ``Query`` keyed ``PK = STUDY#<object_id>``
with a ``begins_with`` sort-key condition, or a bounded per-stream ``Query`` on
``STUDY#<object_id>#S#<stream>`` -- so the page cost is ``1 + #streams`` Queries,
independent of participant count.

Credentials: the web server assumes the least-privilege ``MetadataIndexReaderRole``
(Query + GetItem only) via STS, using **refreshable** credentials so a long-lived
gunicorn worker never serves an expired client. The web principal must be granted
``sts:AssumeRole`` on the role ARN (see METADATA_INDEX.md).

Failure model -- three typed signals the view maps to distinct page states:
  * MetadataIndexNotConfigured -- feature disabled or settings missing (no AWS call)
  * MetadataIndexInvalidStudy  -- the study object_id is malformed (no AWS call)
  * MetadataIndexReadError     -- DynamoDB/STS failed (throttle, assume-role denied)
An empty index is NOT an error -- study_summary returns zero-filled structures.

PII: this module must not log Query responses or exception payloads (they carry
patient_ids). It logs by reason/error-code only.
"""
from __future__ import annotations

import logging
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import get_session

from config.settings import (BEIWE_SERVER_AWS_ACCESS_KEY_ID, BEIWE_SERVER_AWS_SECRET_ACCESS_KEY,
    METADATA_INDEX_ENABLED, METADATA_INDEX_READER_ROLE_ARN, METADATA_INDEX_REGION,
    METADATA_INDEX_TABLE_NAME)

logger = logging.getLogger(__name__)

FEED_LIMIT = 15  # recently-active rows shown on the page
HEATMAP_DAYS = 60  # trailing-day window for the adherence heatmap
HEATMAP_MAX_ROWS = 100  # cap heatmap rows for very large studies; report the remainder

# MUST match cluster_management/cdk/lambdas/metadata_index/dynamo_writer.py:SHARDS
# (and _shard_for). The writer persists its value to the CONFIG item; _assert_shards
# fails loud on drift so a mismatch can't silently drop whole shards of participants.
SHARDS = 8

_LATEST_PREFIX = "LATEST#P#"
_DAY_PREFIX = "DAY#"
_DAILY_SK_PREFIX = "P#"        # SK prefix within a STUDY#<obj>#DAILY#<shard> partition
_FIRST_PREFIX = "FIRST#P#"


def _shard_for(patient: str) -> int:
    """Vendored copy of the writer's shard function -- must stay byte-identical."""
    return zlib.crc32(patient.encode()) % SHARDS


class MetadataIndexNotConfigured(Exception):
    """The feature is disabled or required settings are missing."""


class MetadataIndexInvalidStudy(Exception):
    """The provided study object_id is not a well-formed 24-char Beiwe id."""


class MetadataIndexReadError(Exception):
    """DynamoDB / STS read failed (throttle, assume-role denied, etc.)."""


# --- credentials + table handle (module-global, lazy, refreshable) -----------

_TABLE = None  # cached boto3 DynamoDB Table resource; rebuilt lazily


def _assume_role_credentials() -> dict:
    """Assume the reader role and return botocore-shaped refreshable-credential
    metadata. Called both for the initial fetch and on auto-refresh."""
    sts = boto3.client(
        "sts",
        aws_access_key_id=BEIWE_SERVER_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=BEIWE_SERVER_AWS_SECRET_ACCESS_KEY,
        region_name=METADATA_INDEX_REGION,
    )
    resp = sts.assume_role(
        RoleArn=METADATA_INDEX_READER_ROLE_ARN,
        RoleSessionName="beiwe-metadata-dashboard",
    )
    c = resp["Credentials"]
    return {
        "access_key": c["AccessKeyId"],
        "secret_key": c["SecretAccessKey"],
        "token": c["SessionToken"],
        "expiry_time": c["Expiration"].isoformat(),
    }


def _build_table():
    """Build a DynamoDB Table resource backed by auto-refreshing assume-role creds."""
    credentials = RefreshableCredentials.create_from_metadata(
        metadata=_assume_role_credentials(),
        refresh_using=_assume_role_credentials,
        method="sts-assume-role",
    )
    botocore_session = get_session()
    botocore_session._credentials = credentials
    botocore_session.set_config_variable("region", METADATA_INDEX_REGION)
    session = boto3.Session(botocore_session=botocore_session)
    return session.resource("dynamodb").Table(METADATA_INDEX_TABLE_NAME)


def _get_table():
    """Return the cached Table resource, building it on first use. Tests patch this
    (or ``_query`` / ``study_summary``) so the build path never runs without AWS."""
    global _TABLE
    if _TABLE is None:
        _TABLE = _build_table()
    return _TABLE


# --- pure helpers (no AWS) ---------------------------------------------------

def _num(v) -> int:
    """DynamoDB resource returns Decimal; coerce to int, tolerate None/garbage."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def human_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _parse_time(iso: str):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Defensive: a stored time without a tz designator would make `now - dt` raise
    # (naive vs aware), an uncaught error outside the typed-failure model. Treat
    # naive as UTC (the writer's upload_time basis).
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def ago(iso: str, now: datetime) -> str:
    dt = _parse_time(iso)
    if dt is None:
        return "?"
    secs = max(0, (now - dt).total_seconds())
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _is_stale(iso: str, now: datetime, stale_seconds: int) -> bool:
    dt = _parse_time(iso)
    return dt is None or (now - dt).total_seconds() >= stale_seconds


def is_valid_object_id(object_id) -> bool:
    return isinstance(object_id, str) and len(object_id) == 24 and object_id.isalnum()


# --- aggregation (pure; operates on already-fetched items) -------------------

def aggregate_daily(items: list) -> dict:
    """Sum DAY# rollup items into {day: {count, bytes}}. Accepts items from one
    partition or several merged together (e.g. all per-stream rollups), summing
    per day -- so the study daily trend can be derived from the per-stream rollups."""
    out = {}
    for it in items:
        sk = it.get("SK", "")
        if sk.startswith(_DAY_PREFIX):
            day = sk[len(_DAY_PREFIX):]
            bucket = out.setdefault(day, {"count": 0, "bytes": 0})
            bucket["count"] += _num(it.get("count"))
            bucket["bytes"] += _num(it.get("bytes"))
    return out


def aggregate_latest(items: list) -> tuple[dict, dict]:
    """STUDY#<obj> / LATEST#P#... pointers -> (participant_latest, stream_latest).

    participant_latest: {patient: item}; stream_latest: {(patient, stream): item}.
    Distinguished by the presence of '#S#' in the SK.
    """
    participant_latest, stream_latest = {}, {}
    for it in items:
        sk = it.get("SK", "")
        if not sk.startswith(_LATEST_PREFIX):
            continue
        rest = sk[len(_LATEST_PREFIX):]
        if "#S#" in rest:
            patient, stream = rest.split("#S#", 1)
            stream_latest[(patient, stream)] = it
        else:
            participant_latest[rest] = it
    return participant_latest, stream_latest


def aggregate_participant_daily(items: list) -> dict:
    """Merge sharded participant-daily items (SK P#<patient>#DAY#<day>) into a
    {patient: {day: {count, bytes}}} matrix. Accepts the union of all shard queries."""
    matrix = {}
    for it in items:
        sk = it.get("SK", "")
        if not sk.startswith(_DAILY_SK_PREFIX) or "#DAY#" not in sk:
            continue
        patient, day = sk[len(_DAILY_SK_PREFIX):].split("#DAY#", 1)
        matrix.setdefault(patient, {})[day] = {"count": _num(it.get("count")), "bytes": _num(it.get("bytes"))}
    return matrix


def build_adherence(matrix: dict, first_seen: dict, now: datetime,
                    days_window: int = HEATMAP_DAYS, max_rows: int = HEATMAP_MAX_ROWS) -> dict:
    """Shape the participant×day matrix into a windowed heatmap (pure).

    Each cell is one of: 'data' (uploaded that day), 'pre' (before the participant's
    first_day -> not yet enrolled), or 'zero' (enrolled but no upload). Returns the
    capped heatmap rows plus a full (uncapped) per-patient spark series for the
    freshness sparklines."""
    today = now.date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(days_window - 1, -1, -1)]
    patients = sorted(set(matrix) | set(first_seen))

    rows, spark_by_patient, peak = [], {}, 0
    for p in patients:
        pdays, fd, cells, spark, last_active = matrix.get(p, {}), first_seen.get(p), [], [], ""
        for d in days:
            cell = pdays.get(d)
            count = cell["count"] if cell else 0
            if count > 0:
                state, last_active = "data", d
            elif fd and d < fd:
                state = "pre"
            else:
                state = "zero"
            peak = max(peak, count)
            cells.append({"day": d, "count": count, "state": state})
            spark.append(count)
        spark_by_patient[p] = spark
        rows.append({"patient": p, "first_day": fd, "cells": cells,
                     "row_total": sum(spark), "last_active": last_active})

    omitted = 0
    if len(rows) > max_rows:  # keep the most-recently-active; report the rest
        rows.sort(key=lambda r: (r["last_active"], r["row_total"]), reverse=True)
        omitted, rows = len(rows) - max_rows, rows[:max_rows]
    rows.sort(key=lambda r: r["patient"])  # stable display order
    return {"available": bool(matrix), "days": days, "rows": rows, "peak": peak,
            "omitted": omitted, "spark_by_patient": spark_by_patient}


def build_enrollment(first_seen: dict) -> dict:
    """Cumulative participants by first-upload day (pure)."""
    by_day = Counter(first_seen.values())
    points, cum = [], 0
    for day in sorted(by_day):
        cum += by_day[day]
        points.append({"day": day, "cumulative": cum})
    return {"total": len(first_seen), "points": points}


# --- the one public entry point ----------------------------------------------

def study_summary(study_object_id: str, stale_hours: int = 24, now: datetime | None = None) -> dict:
    """Return the aggregated per-study dashboard views, or raise a typed signal.

    Raises MetadataIndexNotConfigured / MetadataIndexInvalidStudy /
    MetadataIndexReadError. An empty index returns zero-filled structures.
    """
    if not (METADATA_INDEX_ENABLED and METADATA_INDEX_TABLE_NAME and METADATA_INDEX_READER_ROLE_ARN):
        raise MetadataIndexNotConfigured()
    if not is_valid_object_id(study_object_id):
        # Never interpolate an unvalidated id into a key expression.
        raise MetadataIndexInvalidStudy()

    now = now or datetime.now(dt_timezone.utc)
    stale_seconds = stale_hours * 3600
    pk = f"STUDY#{study_object_id}"

    try:
        _assert_shards()  # loud failure on writer/reader shard drift
        latest_items = _query(pk, _LATEST_PREFIX)
        participant_latest, stream_latest = aggregate_latest(latest_items)
        streams = sorted({stream for (_patient, stream) in stream_latest})
        # bounded per-stream fan-out: one Query per stream that has a pointer
        stream_rollups = {
            stream: _query(f"STUDY#{study_object_id}#S#{stream}", _DAY_PREFIX)
            for stream in streams
        }
        # sharded participant-daily matrix: one Query per shard (bounded by SHARDS)
        daily_items = []
        for shard in range(SHARDS):
            daily_items.extend(_query(f"STUDY#{study_object_id}#DAILY#{shard}", _DAILY_SK_PREFIX))
        # first-seen day per participant (enrollment)
        first_seen = {}
        for it in _query(pk, _FIRST_PREFIX):
            patient = it.get("SK", "")[len(_FIRST_PREFIX):]
            if patient and it.get("first_day"):
                first_seen[patient] = it["first_day"]
    except (ClientError, BotoCoreError) as e:
        # Log by reason only -- never the response (it carries patient_ids) or the role ARN.
        logger.warning("metadata index read failed: %s", type(e).__name__)
        raise MetadataIndexReadError() from e

    # Derive the study daily trend + totals from the per-stream rollups (always
    # written) rather than the WRITE_STUDY_ROLLUP-gated STUDY#<obj>/DAY# rollup, so
    # the page stays correct when that optional study-level rollup is disabled.
    daily = aggregate_daily([row for rows in stream_rollups.values() for row in rows])
    views = _build_views(daily, participant_latest, stream_latest, stream_rollups, now, stale_seconds, stale_hours)

    # Tier 2 widgets: adherence heatmap, per-participant sparklines, enrollment.
    adherence = build_adherence(aggregate_participant_daily(daily_items), first_seen, now)
    spark_by_patient = adherence.pop("spark_by_patient")
    for prow in views["participants"]:
        prow["spark"] = spark_by_patient.get(prow["patient"], [])
    views["adherence"] = adherence
    views["enrollment"] = build_enrollment(first_seen)
    return views


def _build_views(daily, participant_latest, stream_latest, stream_rollups, now, stale_seconds, stale_hours) -> dict:
    """Shape the raw aggregates into the template-ready view dict (pure)."""
    # top-line stats
    total_count = sum(d["count"] for d in daily.values())
    total_bytes = sum(d["bytes"] for d in daily.values())
    days_sorted = sorted(daily)
    streams = sorted({stream for (_patient, stream) in stream_latest})

    # daily trend (ascending), with a peak for bar scaling
    peak = max((daily[d]["count"] for d in days_sorted), default=0)
    daily_view = [
        {"day": d, "count": daily[d]["count"], "bytes": daily[d]["bytes"],
         "bytes_h": human_bytes(daily[d]["bytes"]),
         "pct": round(daily[d]["count"] / peak * 100) if peak else 0}
        for d in days_sorted
    ]

    # per-participant freshness (last-upload + stale flag only -- no cumulative totals)
    participants_view = []
    for patient in sorted(participant_latest):
        p = participant_latest[patient]
        p_iso = p.get("last_upload_time", "")
        streams_for_patient = sorted(s for (pt, s) in stream_latest if pt == patient)
        stream_rows = []
        for s in streams_for_patient:
            it = stream_latest[(patient, s)]
            s_iso = it.get("last_upload_time", "")
            stream_rows.append({
                "stream": s,
                "last_upload_time": s_iso,
                "ago": ago(s_iso, now),
                "stale": _is_stale(s_iso, now, stale_seconds),
                "last_size": _num(it.get("last_size")),
                "last_size_h": human_bytes(_num(it.get("last_size"))),
            })
        participants_view.append({
            "patient": patient,
            "last_upload_time": p_iso,
            "ago": ago(p_iso, now),
            "stale": _is_stale(p_iso, now, stale_seconds),
            "streams": stream_rows,
        })

    # recently-active feed: top N participant+stream pointers by recency
    feed = sorted(
        ({"patient": pt, "stream": s, "last_upload_time": it.get("last_upload_time", ""),
          "ago": ago(it.get("last_upload_time", ""), now),
          "last_size_h": human_bytes(_num(it.get("last_size")))}
         for (pt, s), it in stream_latest.items()),
        key=lambda r: r["last_upload_time"], reverse=True,
    )[:FEED_LIMIT]

    # per-stream study totals (bounded read from the new rollup)
    stream_totals = []
    for s in streams:
        rows = stream_rollups.get(s, [])
        count = sum(_num(r.get("count")) for r in rows)
        size = sum(_num(r.get("bytes")) for r in rows)
        last_iso = max((stream_latest[(pt, st)].get("last_upload_time", "")
                        for (pt, st) in stream_latest if st == s), default="")
        stream_totals.append({
            "stream": s, "count": count, "bytes": size, "bytes_h": human_bytes(size),
            "last_upload_time": last_iso, "ago": ago(last_iso, now),
            "stale": _is_stale(last_iso, now, stale_seconds),
        })

    return {
        "stats": {
            "participants": len(participant_latest),
            "streams": len(streams),
            "uploads": total_count,
            "bytes": total_bytes,
            "bytes_h": human_bytes(total_bytes),
            "first_day": days_sorted[0] if days_sorted else None,
            "last_day": days_sorted[-1] if days_sorted else None,
        },
        "daily": daily_view,
        "participants": participants_view,
        "feed": feed,
        "stream_totals": stream_totals,
        "stale_hours": stale_hours,
        "has_data": bool(participant_latest or daily),
    }


# --- DynamoDB Query (begins_with only; paginated; never Scan) -----------------

def _assert_shards() -> None:
    """GetItem the persisted CONFIG/SHARDS and assert it matches our compiled SHARDS,
    so a writer/reader shard-count drift fails LOUD (read-error) instead of silently
    dropping whole shards of participants. An absent CONFIG (bootstrap) is tolerated."""
    item = _get_table().get_item(Key={"PK": "CONFIG", "SK": "SHARDS"}).get("Item")
    if item is not None and _num(item.get("value")) != SHARDS:
        logger.warning("metadata index shard drift: reader=%d config=%d", SHARDS, _num(item.get("value")))
        raise MetadataIndexReadError()


def _query(pk: str, sk_prefix: str) -> list:
    """Query one partition by SK prefix, draining pagination. Query only -- never Scan."""
    table = _get_table()
    items, kwargs = [], {}
    while True:
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix),
            **kwargs,
        )
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek
