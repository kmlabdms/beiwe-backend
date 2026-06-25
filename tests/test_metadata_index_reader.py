"""Tests for libs/metadata_index_reader.py -- the per-study DynamoDB read layer.

No AWS / no moto: the pure aggregation path is exercised against canned Query
responses, and the table handle / _query are patched. Mirrors the libs/s3.py
convention of keeping AWS out of the Django test suite.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

import libs.metadata_index_reader as reader


OID = "a" * 24                      # a well-formed 24-char study object_id
PK = f"STUDY#{OID}"
NOW = datetime(2026, 5, 28, 21, 0, 0, tzinfo=timezone.utc)


def _configured():
    """Context manager: make the feature look enabled + configured."""
    return patch.multiple(
        reader,
        METADATA_INDEX_ENABLED=True,
        METADATA_INDEX_TABLE_NAME="tbl",
        METADATA_INDEX_READER_ROLE_ARN="arn:aws:iam::1:role/reader",
    )


CANNED = {
    (PK, "DAY#"): [
        {"SK": "DAY#2026-05-27", "count": 2, "bytes": 200},
        {"SK": "DAY#2026-05-28", "count": 3, "bytes": 300},
    ],
    (PK, "LATEST#P#"): [
        {"SK": "LATEST#P#p1", "last_upload_time": "2026-05-28T20:00:00Z", "last_stream": "gps", "last_size": 50},
        {"SK": "LATEST#P#p1#S#gps", "last_upload_time": "2026-05-28T20:00:00Z", "last_size": 50},
        {"SK": "LATEST#P#p1#S#accelerometer", "last_upload_time": "2026-05-20T00:00:00Z", "last_size": 10},
        {"SK": "LATEST#P#p2", "last_upload_time": "2026-05-28T19:00:00Z", "last_stream": "gps", "last_size": 5},
        {"SK": "LATEST#P#p2#S#gps", "last_upload_time": "2026-05-28T19:00:00Z", "last_size": 5},
    ],
    (f"STUDY#{OID}#S#gps", "DAY#"): [{"SK": "DAY#2026-05-28", "count": 4, "bytes": 255}],
    (f"STUDY#{OID}#S#accelerometer", "DAY#"): [{"SK": "DAY#2026-05-20", "count": 1, "bytes": 10}],
}


class TestAggregationHelpers(SimpleTestCase):
    def test_aggregate_daily(self):
        out = reader.aggregate_daily(CANNED[(PK, "DAY#")])
        self.assertEqual(out["2026-05-28"], {"count": 3, "bytes": 300})

    def test_aggregate_latest_splits_participant_and_stream_pointers(self):
        participant, stream = reader.aggregate_latest(CANNED[(PK, "LATEST#P#")])
        self.assertEqual(set(participant), {"p1", "p2"})
        self.assertIn(("p1", "gps"), stream)
        self.assertIn(("p1", "accelerometer"), stream)

    def test_object_id_validation(self):
        self.assertTrue(reader.is_valid_object_id(OID))
        self.assertFalse(reader.is_valid_object_id("short"))
        self.assertFalse(reader.is_valid_object_id(f"{OID[:-1]}#"))  # 24 chars but not alnum
        self.assertFalse(reader.is_valid_object_id(None))


class TestStudySummary(SimpleTestCase):
    def test_happy_path_aggregates(self):
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: CANNED[(pk, prefix)]):
            data = reader.study_summary(OID, stale_hours=24, now=NOW)

        self.assertEqual(data["stats"]["participants"], 2)
        self.assertEqual(data["stats"]["streams"], 2)
        self.assertEqual(data["stats"]["uploads"], 5)        # 2 + 3
        self.assertEqual(data["stats"]["bytes"], 500)
        self.assertEqual(data["stats"]["first_day"], "2026-05-27")
        self.assertEqual(data["stats"]["last_day"], "2026-05-28")
        self.assertTrue(data["has_data"])

        totals = {s["stream"]: s for s in data["stream_totals"]}
        self.assertEqual(totals["gps"]["count"], 4)          # from the new per-stream rollup
        self.assertEqual(totals["gps"]["bytes"], 255)
        self.assertEqual(totals["accelerometer"]["count"], 1)

    def test_freshness_has_no_cumulative_totals(self):
        # the freshness rows carry last-upload + stale + last_size only (bounded-read boundary)
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: CANNED[(pk, prefix)]):
            data = reader.study_summary(OID, now=NOW)
        row = data["participants"][0]["streams"][0]
        self.assertNotIn("count", row)
        self.assertNotIn("bytes", row)
        self.assertIn("last_size", row)

    def test_stale_flags(self):
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: CANNED[(pk, prefix)]):
            data = reader.study_summary(OID, stale_hours=24, now=NOW)
        p1 = next(p for p in data["participants"] if p["patient"] == "p1")
        gps = next(r for r in p1["streams"] if r["stream"] == "gps")
        accel = next(r for r in p1["streams"] if r["stream"] == "accelerometer")
        self.assertFalse(gps["stale"])     # uploaded 1h ago
        self.assertTrue(accel["stale"])    # last upload 8 days ago

    def test_feed_sorted_newest_first_and_capped(self):
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: CANNED[(pk, prefix)]):
            data = reader.study_summary(OID, now=NOW)
        times = [r["last_upload_time"] for r in data["feed"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertLessEqual(len(data["feed"]), reader.FEED_LIMIT)

    def test_bounded_reads_one_query_per_stream(self):
        calls = []
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: calls.append(pk) or CANNED[(pk, prefix)]):
            reader.study_summary(OID, now=NOW)
        per_stream = [pk for pk in calls if "#S#" in pk]
        self.assertEqual(len(per_stream), 2)               # exactly one Query per stream

    def test_empty_index_returns_zero_filled(self):
        with _configured(), patch.object(reader, "_query", side_effect=lambda pk, prefix: []):
            data = reader.study_summary(OID, now=NOW)
        self.assertEqual(data["stats"]["participants"], 0)
        self.assertEqual(data["stats"]["uploads"], 0)
        self.assertEqual(data["participants"], [])
        self.assertEqual(data["feed"], [])
        self.assertFalse(data["has_data"])

    def test_not_configured_raises_without_aws_call(self):
        with patch.object(reader, "METADATA_INDEX_ENABLED", False), \
             patch.object(reader, "_query") as q:
            with self.assertRaises(reader.MetadataIndexNotConfigured):
                reader.study_summary(OID, now=NOW)
        q.assert_not_called()

    def test_invalid_study_raises_without_aws_call(self):
        with _configured(), patch.object(reader, "_query") as q:
            with self.assertRaises(reader.MetadataIndexInvalidStudy):
                reader.study_summary("not-24-chars", now=NOW)
        q.assert_not_called()

    def test_read_error_is_typed_and_logs_no_pii(self):
        boom = ClientError({"Error": {"Code": "ThrottlingException"}}, "Query")
        with _configured(), patch.object(reader, "_query", side_effect=boom):
            with self.assertLogs(reader.logger, level="WARNING") as logs:
                with self.assertRaises(reader.MetadataIndexReadError):
                    reader.study_summary(OID, now=NOW)
        # logged by reason only -- no patient ids, no role ARN
        joined = " ".join(logs.output)
        self.assertNotIn("arn:aws:iam", joined)
        self.assertNotIn("p1", joined)


class TestQueryNeverScans(SimpleTestCase):
    class _PagedTable:
        """Serves preconfigured query pages; .scan raises to prove it is never used."""
        def __init__(self, pages):
            self._pages = list(pages)
            self.query_count = 0

        def query(self, **kwargs):
            self.query_count += 1
            return self._pages.pop(0)

        def scan(self, **kwargs):
            raise AssertionError("Scan must never be called")

    def test_query_drains_pagination_and_never_scans(self):
        table = self._PagedTable([
            {"Items": [{"PK": "x", "SK": "DAY#1"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"PK": "x", "SK": "DAY#2"}]},
        ])
        with patch.object(reader, "_get_table", return_value=table):
            items = reader._query("STUDY#x", "DAY#")
        self.assertEqual(len(items), 2)
        self.assertEqual(table.query_count, 2)   # paginated through both pages

    def test_study_summary_path_never_calls_scan(self):
        table = self._PagedTable([{"Items": []}] * 10)  # every query returns empty
        with _configured(), patch.object(reader, "_get_table", return_value=table):
            data = reader.study_summary(OID, now=NOW)   # would raise AssertionError if scan used
        self.assertFalse(data["has_data"])
