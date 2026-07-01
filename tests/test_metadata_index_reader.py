"""Tests for libs/metadata_index_reader.py -- the per-study DynamoDB read layer.

No AWS / no moto: the pure aggregation path is exercised against canned Query
responses, and the table handle / _query / _assert_shards are patched. Mirrors the
libs/s3.py convention of keeping AWS out of the Django test suite.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

import libs.metadata_index_reader as reader


OID = "a" * 24                      # a well-formed 24-char study object_id
PK = f"STUDY#{OID}"
NOW = datetime(2026, 5, 28, 21, 0, 0, tzinfo=timezone.utc)


def _configured():
    return patch.multiple(
        reader,
        METADATA_INDEX_ENABLED=True,
        METADATA_INDEX_TABLE_NAME="tbl",
        METADATA_INDEX_READER_ROLE_ARN="arn:aws:iam::1:role/reader",
    )


# Per-stream rollups -> study daily totals (gps 3/250, accel 2/250 = 5/500).
CANNED = {
    (PK, "LATEST#P#"): [
        {"SK": "LATEST#P#p1", "last_upload_time": "2026-05-28T20:00:00Z", "last_stream": "gps", "last_size": 50},
        {"SK": "LATEST#P#p1#S#gps", "last_upload_time": "2026-05-28T20:00:00Z", "last_size": 50},
        {"SK": "LATEST#P#p1#S#accelerometer", "last_upload_time": "2026-05-20T00:00:00Z", "last_size": 10},
        {"SK": "LATEST#P#p2", "last_upload_time": "2026-05-28T19:00:00Z", "last_stream": "gps", "last_size": 5},
        {"SK": "LATEST#P#p2#S#gps", "last_upload_time": "2026-05-28T19:00:00Z", "last_size": 5},
    ],
    (f"STUDY#{OID}#S#gps", "DAY#"): [
        {"SK": "DAY#2026-05-27", "count": 2, "bytes": 200},
        {"SK": "DAY#2026-05-28", "count": 1, "bytes": 50},
    ],
    (f"STUDY#{OID}#S#accelerometer", "DAY#"): [{"SK": "DAY#2026-05-28", "count": 2, "bytes": 250}],
    (PK, "FIRST#P#"): [
        {"SK": "FIRST#P#p1", "first_day": "2026-05-20"},
        {"SK": "FIRST#P#p2", "first_day": "2026-05-28"},
    ],
}
# Sharded participant-daily items, placed in the shard the reader's _shard_for picks.
for _patient, _day, _c, _b in [("p1", "2026-05-28", 3, 350), ("p1", "2026-05-27", 2, 200),
                               ("p2", "2026-05-28", 1, 50)]:
    _pk = f"STUDY#{OID}#DAILY#{reader._shard_for(_patient)}"
    CANNED.setdefault((_pk, "P#"), []).append(
        {"SK": f"P#{_patient}#DAY#{_day}", "count": _c, "bytes": _b})


def _canned(pk, prefix):
    return CANNED.get((pk, prefix), [])


def _summary_env(query_side_effect=_canned):
    """Patch config + a no-op _assert_shards (tested separately) + _query."""
    stack = ExitStack()
    stack.enter_context(_configured())
    stack.enter_context(patch.object(reader, "_assert_shards"))
    stack.enter_context(patch.object(reader, "_query", side_effect=query_side_effect))
    return stack


class TestAggregationHelpers(SimpleTestCase):
    def test_aggregate_daily_sums_across_partitions(self):
        merged = reader.aggregate_daily([
            {"SK": "DAY#2026-05-28", "count": 1, "bytes": 50},
            {"SK": "DAY#2026-05-28", "count": 2, "bytes": 250},
        ])
        self.assertEqual(merged["2026-05-28"], {"count": 3, "bytes": 300})

    def test_aggregate_latest_splits_participant_and_stream_pointers(self):
        participant, stream = reader.aggregate_latest(CANNED[(PK, "LATEST#P#")])
        self.assertEqual(set(participant), {"p1", "p2"})
        self.assertIn(("p1", "gps"), stream)

    def test_aggregate_participant_daily_builds_matrix(self):
        items = [{"SK": "P#p1#DAY#2026-05-28", "count": 3, "bytes": 350},
                 {"SK": "P#p2#DAY#2026-05-28", "count": 1, "bytes": 50}]
        m = reader.aggregate_participant_daily(items)
        self.assertEqual(m["p1"]["2026-05-28"], {"count": 3, "bytes": 350})
        self.assertIn("p2", m)

    def test_object_id_validation(self):
        self.assertTrue(reader.is_valid_object_id(OID))
        self.assertFalse(reader.is_valid_object_id("short"))
        self.assertFalse(reader.is_valid_object_id(f"{OID[:-1]}#"))
        self.assertFalse(reader.is_valid_object_id(None))


class TestBuildAdherence(SimpleTestCase):
    def test_cell_states_and_peak(self):
        matrix = {"p1": {"2026-05-28": {"count": 3, "bytes": 350},
                         "2026-05-27": {"count": 2, "bytes": 200}}}
        first_seen = {"p1": "2026-05-20"}
        adh = reader.build_adherence(matrix, first_seen, NOW)
        self.assertTrue(adh["available"])
        self.assertEqual(len(adh["days"]), reader.HEATMAP_DAYS)
        self.assertEqual(adh["days"][-1], "2026-05-28")
        self.assertEqual(adh["peak"], 3)
        cells = {c["day"]: c for c in adh["rows"][0]["cells"]}
        self.assertEqual(cells["2026-05-28"]["state"], "data")
        self.assertEqual(cells["2026-05-19"]["state"], "pre")    # before first_day
        self.assertEqual(cells["2026-05-25"]["state"], "zero")   # enrolled, no upload

    def test_empty_matrix_not_available(self):
        adh = reader.build_adherence({}, {}, NOW)
        self.assertFalse(adh["available"])
        self.assertEqual(adh["rows"], [])

    def test_row_cap(self):
        matrix = {f"p{i:03d}": {"2026-05-28": {"count": 1, "bytes": 1}} for i in range(120)}
        adh = reader.build_adherence(matrix, {}, NOW, max_rows=100)
        self.assertEqual(len(adh["rows"]), 100)
        self.assertEqual(adh["omitted"], 20)


class TestBuildEnrollment(SimpleTestCase):
    def test_cumulative_by_first_day(self):
        e = reader.build_enrollment({"p1": "2026-05-20", "p2": "2026-05-20", "p3": "2026-05-28"})
        self.assertEqual(e["total"], 3)
        self.assertEqual(e["points"], [{"day": "2026-05-20", "cumulative": 2},
                                       {"day": "2026-05-28", "cumulative": 3}])


class TestStudySummary(SimpleTestCase):
    def test_happy_path_aggregates(self):
        with _summary_env():
            data = reader.study_summary(OID, stale_hours=24, now=NOW)
        self.assertEqual(data["stats"]["participants"], 2)
        self.assertEqual(data["stats"]["uploads"], 5)
        self.assertEqual(data["stats"]["bytes"], 500)
        totals = {s["stream"]: s for s in data["stream_totals"]}
        self.assertEqual(totals["gps"]["count"], 3)
        self.assertEqual(totals["accelerometer"]["count"], 2)

    def test_adherence_and_sparklines(self):
        with _summary_env():
            data = reader.study_summary(OID, now=NOW)
        self.assertTrue(data["adherence"]["available"])
        rows = {r["patient"]: r for r in data["adherence"]["rows"]}
        self.assertIn("p1", rows)
        # p1's daily series sums to 5 (3 + 2); attached to the freshness row too
        p1_view = next(p for p in data["participants"] if p["patient"] == "p1")
        self.assertEqual(sum(p1_view["spark"]), 5)
        self.assertEqual(len(p1_view["spark"]), reader.HEATMAP_DAYS)

    def test_enrollment(self):
        with _summary_env():
            data = reader.study_summary(OID, now=NOW)
        self.assertEqual(data["enrollment"]["total"], 2)
        self.assertEqual(data["enrollment"]["points"][-1], {"day": "2026-05-28", "cumulative": 2})

    def test_freshness_has_no_cumulative_totals(self):
        with _summary_env():
            data = reader.study_summary(OID, now=NOW)
        row = data["participants"][0]["streams"][0]
        self.assertNotIn("count", row)
        self.assertIn("last_size", row)

    def test_bounded_reads(self):
        calls = []

        def se(pk, prefix):
            calls.append((pk, prefix))
            return _canned(pk, prefix)

        with _summary_env(se):
            reader.study_summary(OID, now=NOW)
        prefixes = [p for (_pk, p) in calls]
        self.assertEqual(prefixes.count("LATEST#P#"), 1)
        self.assertEqual(len([pk for (pk, p) in calls if "#S#" in pk and p == "DAY#"]), 2)  # one per stream
        self.assertEqual(prefixes.count("P#"), reader.SHARDS)                                # one per shard
        self.assertEqual(prefixes.count("FIRST#P#"), 1)

    def test_empty_index_returns_zero_filled(self):
        with _summary_env(lambda pk, prefix: []):
            data = reader.study_summary(OID, now=NOW)
        self.assertEqual(data["stats"]["participants"], 0)
        self.assertFalse(data["has_data"])
        self.assertFalse(data["adherence"]["available"])
        self.assertEqual(data["enrollment"]["total"], 0)

    def test_not_configured_raises_without_aws_call(self):
        with patch.object(reader, "METADATA_INDEX_ENABLED", False), patch.object(reader, "_query") as q:
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
        with _summary_env(boom):
            with self.assertLogs(reader.logger, level="WARNING") as logs:
                with self.assertRaises(reader.MetadataIndexReadError):
                    reader.study_summary(OID, now=NOW)
        joined = " ".join(logs.output)
        self.assertNotIn("arn:aws:iam", joined)
        self.assertNotIn("p1", joined)


class TestShardAssert(SimpleTestCase):
    class _ConfigTable:
        def __init__(self, value):
            self.value = value

        def get_item(self, Key=None):
            if self.value is None:
                return {}
            return {"Item": {"PK": "CONFIG", "SK": "SHARDS", "value": self.value}}

    def test_match_passes(self):
        with patch.object(reader, "_get_table", return_value=self._ConfigTable(reader.SHARDS)):
            reader._assert_shards()  # no raise

    def test_mismatch_raises_loud(self):
        with patch.object(reader, "_get_table", return_value=self._ConfigTable(reader.SHARDS + 1)):
            with self.assertRaises(reader.MetadataIndexReadError):
                reader._assert_shards()

    def test_absent_config_tolerated(self):
        with patch.object(reader, "_get_table", return_value=self._ConfigTable(None)):
            reader._assert_shards()  # bootstrap: no raise


class TestQueryNeverScans(SimpleTestCase):
    class _PagedTable:
        """Serves preconfigured query pages; .scan raises to prove it is never used."""
        def __init__(self, pages):
            self._pages = list(pages)
            self.query_count = 0

        def query(self, **kwargs):
            self.query_count += 1
            return self._pages.pop(0) if self._pages else {"Items": []}

        def get_item(self, **kwargs):
            return {}   # absent CONFIG -> tolerated

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
        self.assertEqual(table.query_count, 2)

    def test_study_summary_path_never_calls_scan(self):
        table = self._PagedTable([])  # all queries return empty; get_item -> {}
        with _configured(), patch.object(reader, "_get_table", return_value=table):
            data = reader.study_summary(OID, now=NOW)
        self.assertFalse(data["has_data"])
