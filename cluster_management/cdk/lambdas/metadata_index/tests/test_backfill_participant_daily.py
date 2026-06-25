"""Unit tests for backfill_participant_daily.aggregate_rollups (pure; no AWS).

backfill_participant_daily.py lives in the cdk dir (two levels up); import it the
same way test_show_metadata_index.py does.
"""
import pathlib
import sys

_CDK_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(_CDK_DIR) not in sys.path:
    sys.path.insert(0, str(_CDK_DIR))

import backfill_participant_daily as bf  # noqa: E402

STUDY = "2grtzwKjSxi64uYkxqZASgxe"
OTHER = "abcdefghijklmnopqrstuvwx"


def _rollup(patient, stream, day, count, size):
    return {"PK": f"STUDY#{STUDY}#P#{patient}#S#{stream}", "SK": f"DAY#{day}", "count": count, "bytes": size}


def test_sums_across_streams_and_takes_min_day():
    items = [
        _rollup("p1", "gps", "2026-05-27", 2, 200),
        _rollup("p1", "accelerometer", "2026-05-27", 1, 50),   # same patient+day, other stream -> sums
        _rollup("p1", "gps", "2026-05-28", 3, 300),
        _rollup("p2", "gps", "2026-05-28", 1, 10),
    ]
    daily, first = bf.aggregate_rollups(items)
    assert daily[(STUDY, "p1", "2026-05-27")] == {"count": 3, "bytes": 250}
    assert daily[(STUDY, "p1", "2026-05-28")] == {"count": 3, "bytes": 300}
    assert daily[(STUDY, "p2", "2026-05-28")] == {"count": 1, "bytes": 10}
    assert first[(STUDY, "p1")] == "2026-05-27"   # earliest day
    assert first[(STUDY, "p2")] == "2026-05-28"


def test_ignores_non_perstream_rollup_items():
    items = [
        _rollup("p1", "gps", "2026-05-28", 1, 1),
        {"PK": f"STUDY#{STUDY}#S#gps", "SK": "DAY#2026-05-28", "count": 9, "bytes": 9},          # study-stream
        {"PK": f"STUDY#{STUDY}", "SK": "DAY#2026-05-28", "count": 9, "bytes": 9},                 # study total
        {"PK": f"STUDY#{STUDY}#DAILY#0", "SK": "P#p1#DAY#2026-05-28", "count": 9, "bytes": 9},    # existing participant-daily
        {"PK": "OBJ#whatever", "SK": "OBJ"},                                                       # dedupe marker
        {"PK": f"STUDY#{STUDY}", "SK": "LATEST#P#p1#S#gps", "last_size": 9},                       # latest pointer
    ]
    daily, first = bf.aggregate_rollups(items)
    assert list(first) == [(STUDY, "p1")]                       # only the real rollup contributed
    assert daily[(STUDY, "p1", "2026-05-28")] == {"count": 1, "bytes": 1}

def test_study_filter():
    items = [
        _rollup("p1", "gps", "2026-05-28", 1, 1),
        {"PK": f"STUDY#{OTHER}#P#p9#S#gps", "SK": "DAY#2026-05-28", "count": 5, "bytes": 5},
    ]
    daily, first = bf.aggregate_rollups(items, study_filter={STUDY})
    assert {s for (s, _p, _d) in daily} == {STUDY}
    assert set(first) == {(STUDY, "p1")}


def test_shard_fn_is_deterministic_and_in_range():
    assert bf._shard_for("p1") == bf._shard_for("p1")
    assert 0 <= bf._shard_for("xyz") < bf.SHARDS
