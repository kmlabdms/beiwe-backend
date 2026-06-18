"""Unit tests for the pure key/event parser (U3).

Covers happy paths (one per real mobile token), the key-grammar irregularities
that broke the naive 4-segment design (survey 5-segment, ios/log slash, audio
extensions, -duplicate- suffix, key files), and the malformed/ignored branches.
"""
import parser as p
import pytest

STUDY = "2grtzwKjSxi64uYkxqZASgxe"   # real 24-char object_id shape
PATIENT = "7xhpe54h"                 # real <=8 char patient_id
UPLOAD_TIME = "2026-05-28T20:27:34Z"


def _parse(key, size=1234):
    return p.parse(key=key, size=size, upload_time=UPLOAD_TIME, bucket="raw-bucket")


# --- happy path: one valid key per observed live mobile token ----------------

@pytest.mark.parametrize("token,canonical", [
    ("accel", "accelerometer"),
    ("gps", "gps"),
    ("bluetoothLog", "bluetooth"),
    ("callLog", "calls"),
    ("gyro", "gyro"),
    ("logFile", "app_log"),
    ("powerState", "power_state"),
    ("textsLog", "texts"),
    ("wifiLog", "wifi"),
    ("ambientAudio", "ambient_audio"),
    ("surveyAnswers", "survey_answers"),
])
def test_happy_path_streams(token, canonical):
    key = f"{STUDY}/{PATIENT}/{token}/1779996316436.csv.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.study == STUDY
    assert rec.patient == PATIENT
    assert rec.stream == canonical
    assert rec.size == 1234
    assert rec.upload_time == UPLOAD_TIME
    assert rec.key == key            # full original key retained for dedupe/last_key
    assert rec.device_time == 1779996316  # 13-digit ms -> first 10 digits (epoch s)


# --- the irregularities that broke the naive fixed-split design --------------

def test_survey_five_segment_key():
    # survey_object_id sits BETWEEN the stream token and the timestamp.
    survey_id = "sUrVeY1234567890AbCdEf12"
    key = f"{STUDY}/{PATIENT}/surveyTimings/{survey_id}/1779996316436.csv.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.stream == "survey_timings"
    assert rec.device_time == 1779996316   # timestamp read from the FINAL segment


def test_ios_log_slash_token():
    key = f"{STUDY}/{PATIENT}/ios/log/1779996316436.csv.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.stream == "ios_log"        # not "ios"
    assert rec.device_time == 1779996316


def test_audio_recording_non_csv_extension():
    key = f"{STUDY}/{PATIENT}/voiceRecording/1779996316436.mp4.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.stream == "audio_recordings"
    assert rec.device_time == 1779996316   # parsed despite .mp4


def test_duplicate_suffix_stripped_for_timestamp():
    key = f"{STUDY}/{PATIENT}/accel/1779996316436.csv-duplicate-abc123XYZ.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.stream == "accelerometer"
    assert rec.device_time == 1779996316


# --- timestamp tolerance -----------------------------------------------------

def test_ten_digit_epoch_seconds():
    key = f"{STUDY}/{PATIENT}/gps/1718953200.csv.zst"
    rec = _parse(key)
    assert rec.device_time == 1718953200


def test_non_numeric_timestamp_yields_none_but_still_a_record():
    key = f"{STUDY}/{PATIENT}/gps/notatimestamp.csv.zst"
    rec = _parse(key)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.device_time is None        # upload_time/size are the load-bearing fields


# --- ignored (non-raw) keys --------------------------------------------------

@pytest.mark.parametrize("key", [
    f"CHUNKED_DATA/{STUDY}/{PATIENT}/gps/2026-05-28T20:00:00.csv.zst",
    "LOGS/auth_log/ip-10-0-0-229-2026-06-04T00:00:11.log",
    f"PROBLEM_UPLOADS/{STUDY}/{PATIENT}/gps/1779996316436_abc.zst",
])
def test_non_raw_prefixes_ignored(key):
    out = _parse(key)
    assert isinstance(out, p.Ignore)
    assert out.reason == "non_raw_prefix"


def test_rsa_key_file_ignored_not_malformed():
    # <study>/keys/<patient>_private.zst -- patient="keys" would falsely validate,
    # so this must be Ignore (so it never trips the Malformed alarm), not Malformed.
    key = f"{STUDY}/keys/{PATIENT}_private.zst"
    out = _parse(key)
    assert isinstance(out, p.Ignore)
    assert out.reason == "key_file"


def test_forest_artifact_ignored():
    key = f"{STUDY}/{PATIENT}/forest/something.zst"
    out = _parse(key)
    assert isinstance(out, p.Ignore)
    assert out.reason == "forest"


# --- malformed keys ----------------------------------------------------------

def test_short_participant_level_key_is_too_few_segments():
    key = f"{STUDY}/{PATIENT}.zst"
    out = _parse(key)
    assert isinstance(out, p.Malformed)
    assert out.reason == "too_few_segments"


def test_unknown_stream_token_is_malformed():
    key = f"{STUDY}/{PATIENT}/notarealstream/1779996316436.csv.zst"
    out = _parse(key)
    assert isinstance(out, p.Malformed)
    assert out.reason == "unknown_stream"


def test_bad_study_id_is_malformed():
    key = f"shortstudy/{PATIENT}/accel/1779996316436.csv.zst"
    out = _parse(key)
    assert isinstance(out, p.Malformed)
    assert out.reason == "bad_study_id"


def test_bad_patient_id_is_malformed():
    # uppercase is not allowed in patient_id ([1-9a-z]); use a token that is a
    # known stream at position 1 so stream-resolution succeeds and we reach
    # patient validation.
    key = f"{STUDY}/BADPATIENT/accel/1779996316436.csv.zst"
    out = _parse(key)
    assert isinstance(out, p.Malformed)
    assert out.reason == "bad_patient_id"


# --- URL-encoding ------------------------------------------------------------

def test_url_encoded_key_decodes_before_parsing():
    raw = f"{STUDY}/{PATIENT}/accel/1779996316436.csv.zst"
    encoded = raw.replace("/", "%2F")     # an S3-notification-style encoded key
    rec = _parse(encoded)
    assert isinstance(rec, p.MetadataRecord)
    assert rec.stream == "accelerometer"
    assert rec.study == STUDY
