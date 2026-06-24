"""Pure key/event parser for the Upload Metadata Index.

Turns an S3 object event (bucket, key, size, upload time) into one of three
typed outcomes -- ``MetadataRecord``, ``Ignore``, or ``Malformed`` -- using ONLY
the object key and the event record. It never reads the object body (R2).

It mirrors the authoritative production parser
(``libs/file_processing/utility_functions_simple.py:s3_file_path_to_data_type``):
it scans path segments for a known stream token rather than assuming a fixed
segment position, normalizes ``-duplicate-`` suffixes, and special-cases the
historical ``ios/log`` slash. This is why it survives the real key grammar:

    raw upload : <study_object_id>/<patient_id>/<mobile_token>/<ts>.<ext>.zst
    survey     : <study>/<patient>/surveyTimings/<survey_object_id>/<ts>.csv.zst
    ios log    : <study>/<patient>/ios/log/<ts>.csv.zst
    audio      : <study>/<patient>/voiceRecording/<ts>.mp4.zst
    duplicate  : <...>/<ts>.csv-duplicate-<rand>.zst
    key file   : <study>/keys/<patient>_private.zst        (ignored)

No boto3, no I/O -- fully unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import unquote_plus

from stream_map import DATA_STREAM_NAME_MAPPING, IDENTIFIERS, IOS_LOG_FILE, NON_UPLOAD_STREAMS

# Prefixes that are not raw participant uploads (processed chunks, server logs,
# decryption-failure dumps). Matched on the decoded key.
IGNORE_PREFIXES = ("CHUNKED_DATA/", "LOGS/", "PROBLEM_UPLOADS/")

# Study object_id: exactly 24 chars [a-zA-Z0-9]; patient_id: 1-8 chars [1-9a-z].
_STUDY_RE = re.compile(r"^[a-zA-Z0-9]{24}$")
_PATIENT_RE = re.compile(r"^[1-9a-z]{1,8}$")
_LEADING_DIGITS_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class MetadataRecord:
    """A parsed raw-upload event ready to be written to the index."""
    study: str
    patient: str
    stream: str               # canonical stream name (e.g. "accelerometer")
    device_time: Optional[int]  # epoch SECONDS from the filename, or None if unparseable
    upload_time: str          # S3 event time (ISO-8601), the "last upload" basis
    size: int                 # object size in bytes, from the event record
    key: str                  # full original S3 key (incl. .zst) -- used for dedupe/last_key


@dataclass(frozen=True)
class Ignore:
    """A key that is deliberately not indexed (non-raw prefix, key file, forest)."""
    reason: str


@dataclass(frozen=True)
class Malformed:
    """A key that looks like a raw upload but could not be parsed safely."""
    reason: str


ParsedOutcome = Union[MetadataRecord, Ignore, Malformed]


def _resolve_stream(path: str, segments: list) -> Optional[str]:
    """Return the canonical stream for a key path, or None if unrecognized.

    Mirrors s3_file_path_to_data_type's substring special-cases and identifiers
    fallback, but scans only ``segments[2:]`` for the stream token -- i.e. the
    path AFTER study (segments[0]) and patient (segments[1]). This avoids
    misclassifying a participant whose patient_id happens to equal a stream token
    (e.g. patient_id "gps"/"gyro"/"wifi"); the production parser scans the whole
    path and is vulnerable to that collision, which we deliberately do not inherit.
    """
    # Substring special-cases -- these tokens are not clean single segments.
    if "/keys/" in path:
        return "key_file"
    if "ios/log" in path:
        return IOS_LOG_FILE
    if "forest" in segments[2:]:
        return "forest"
    # Scan only the segments after study + patient against the known-token map.
    for piece in segments[2:]:
        data_type = DATA_STREAM_NAME_MAPPING.get(piece)
        if data_type:
            return data_type
    # Fallback: identifiers files have historically appeared without a clean token.
    if "identifiers" in path:
        return IDENTIFIERS
    return None


def _parse_device_time(final_segment: str) -> Optional[int]:
    """Epoch SECONDS from the leading digits of the final path segment, or None.

    Matches clean_java_timecode's int(s[:10]) behavior: a 13-digit epoch-ms
    timestamp yields its first 10 digits (epoch seconds); a 10-digit epoch-second
    timestamp is taken as-is. Tolerant of any extension (.csv, .mp4, .wav).
    """
    match = _LEADING_DIGITS_RE.match(final_segment)
    if not match:
        return None
    digits = match.group(1)
    try:
        return int(digits[:10])
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


def parse(key: str, size: int, upload_time: str, bucket: Optional[str] = None) -> ParsedOutcome:
    """Parse one S3 object-created event into a typed outcome.

    Args:
        key: the S3 object key (may be URL-encoded if it came from an S3
            notification envelope; EventBridge delivers it raw -- decoding is
            a safe no-op for Beiwe keys, which contain no '+'/'%'/space).
        size: object size in bytes from the event record.
        upload_time: the event time (ISO-8601 string) -- the "last upload" basis.
        bucket: optional bucket name, carried through for context only.
    """
    decoded = unquote_plus(key)

    if decoded.startswith(IGNORE_PREFIXES):
        return Ignore(reason="non_raw_prefix")

    # Strip the historical "-duplicate-<rand>" suffix before any token work, but
    # keep the original key for dedupe / last_key so two genuinely distinct
    # objects never collapse.
    normalized = decoded.split("-duplicate")[0] if "duplicate" in decoded else decoded
    work = normalized[:-4] if normalized.endswith(".zst") else normalized

    segments = work.split("/")
    if len(segments) < 3:
        # e.g. "<study>/<patient>.zst" -- a participant-level object, not an upload.
        # Checked before stream resolution so it reports the precise reason rather
        # than "unknown_stream" (key files are 3 segments and still reach Ignore).
        return Malformed(reason="too_few_segments")

    stream = _resolve_stream(work, segments)
    if stream is None:
        return Malformed(reason="unknown_stream")
    if stream in NON_UPLOAD_STREAMS:
        return Ignore(reason=stream)

    study, patient = segments[0], segments[1]
    if not _STUDY_RE.match(study):
        return Malformed(reason="bad_study_id")
    if not _PATIENT_RE.match(patient):
        return Malformed(reason="bad_patient_id")

    # upload_time is the load-bearing "last upload" basis and the rollup day key.
    # A missing/empty event time would silently produce a bogus "DAY#" bucket and
    # an empty last_upload_time, so reject it rather than writing garbage.
    if not upload_time:
        return Malformed(reason="missing_upload_time")

    device_time = _parse_device_time(segments[-1])
    return MetadataRecord(
        study=study,
        patient=patient,
        stream=stream,
        device_time=device_time,
        upload_time=upload_time,
        size=size,
        key=key,
    )
