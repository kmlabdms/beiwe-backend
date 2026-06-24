"""Regression test for the scan-based ops viewer (`show_metadata_index.py`).

The writer now emits a study-level per-stream rollup (`STUDY#<study>#S#<stream>` /
`DAY#`). Its PK has no `#P#` segment, so the viewer's study-daily branch
(`"#P#" not in pk and ... DAY#`) used to swallow it -- inventing a bogus
`<study>#S#<stream>` "study" and double-counting study totals. aggregate() must
now ignore these items (it already derives per-stream totals from the
participant-scoped rollups).
"""
import pathlib
import sys

# show_metadata_index.py lives in the cdk dir, two levels up from this test;
# the shared conftest only puts the Lambda package dir on sys.path.
_CDK_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(_CDK_DIR) not in sys.path:
    sys.path.insert(0, str(_CDK_DIR))

import show_metadata_index as smi  # noqa: E402

STUDY = "2grtzwKjSxi64uYkxqZASgxe"
DAY = "2026-05-28"


def test_aggregate_ignores_study_stream_rollup():
    items = [
        # real study-level daily rollup (the authoritative study totals)
        {"PK": f"STUDY#{STUDY}", "SK": f"DAY#{DAY}", "count": 3, "bytes": 350},
        # NEW study-level per-stream rollups -- must be ignored by the scan viewer
        {"PK": f"STUDY#{STUDY}#S#gps", "SK": f"DAY#{DAY}", "count": 1, "bytes": 50},
        {"PK": f"STUDY#{STUDY}#S#accelerometer", "SK": f"DAY#{DAY}", "count": 2, "bytes": 300},
    ]
    data = smi.aggregate(items)

    # no bogus "<study>#S#<stream>" study is invented
    assert data["studies"] == {STUDY}
    # study totals reflect only the real study-level rollup -- not double-counted
    assert data["study_daily"][STUDY][DAY] == {"count": 3, "bytes": 350}


def test_aggregate_still_reads_participant_stream_rollup():
    # the participant-scoped rollup (with #P#) must still be aggregated normally
    items = [
        {"PK": f"STUDY#{STUDY}#P#7xhpe54h#S#gps", "SK": f"DAY#{DAY}", "count": 4, "bytes": 99},
    ]
    data = smi.aggregate(items)
    assert data["stream_totals"][(STUDY, "7xhpe54h", "gps")] == {"count": 4, "bytes": 99}
