"""Replay the canonical event fixtures through the handler (U5).

Proves the fixtures are valid and that every key irregularity in the mixed batch
resolves to the expected index state when run end-to-end via the replay harness.
"""
import json
import pathlib

import replay_event

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
STUDY = "2grtzwKjSxi64uYkxqZASgxe"
PATIENT = "7xhpe54h"
DAY = "2026-05-28"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _by_key(items):
    return {(i["PK"], i["SK"]): i for i in items}


def test_eventbridge_fixture_writes_one_record():
    result, items = replay_event.replay(_load("eventbridge_object_created.json"))
    assert result["batchItemFailures"] == []
    index = _by_key(items)
    rollup = index[(f"STUDY#{STUDY}#P#{PATIENT}#S#accelerometer", f"DAY#{DAY}")]
    assert int(rollup["count"]) == 1 and int(rollup["bytes"]) == 100


def test_s3_notification_fixture_unwraps():
    result, items = replay_event.replay(_load("s3_notification.json"))
    assert result["batchItemFailures"] == []
    rollup = _by_key(items)[(f"STUDY#{STUDY}#P#{PATIENT}#S#gps", f"DAY#{DAY}")]
    assert int(rollup["bytes"]) == 77


def test_mixed_batch_exercises_every_irregularity():
    result, items = replay_event.replay(_load("mixed_batch.json"))
    assert result["batchItemFailures"] == []  # nothing retried; malformed/ignored handled inline
    index = _by_key(items)

    def rollup(stream):
        return index.get((f"STUDY#{STUDY}#P#{PATIENT}#S#{stream}", f"DAY#{DAY}"))

    # survey (5-seg), ios/log (slash), audio (.mp4) all parsed; accel duplicate not recounted
    assert int(rollup("accelerometer")["count"]) == 1
    assert int(rollup("survey_timings")["count"]) == 1
    assert int(rollup("ios_log")["count"]) == 1
    assert int(rollup("audio_recordings")["count"]) == 1

    # ignored keys (CHUNKED_DATA, key file) and the malformed key produced no rollups
    assert rollup("gps") is None
    assert rollup("key_file") is None
    assert rollup("notarealstream") is None

    # study-level rollup = 4 unique uploads, 100+50+10+200 bytes
    study = index[(f"STUDY#{STUDY}", f"DAY#{DAY}")]
    assert int(study["count"]) == 4 and int(study["bytes"]) == 360
