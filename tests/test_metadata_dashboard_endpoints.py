from unittest.mock import patch

from constants.user_constants import ResearcherRole
from libs import metadata_index_reader as reader
from tests.common import ResearcherSessionTest


def _summary(patient="p1", has_data=True, adherence_available=True):
    """A canned study_summary() return value matching the template's shape."""
    return {
        "has_data": has_data,
        "stats": {"participants": 1, "streams": 1, "uploads": 3, "bytes": 300,
                  "bytes_h": "300 B", "first_day": "2026-05-27", "last_day": "2026-05-28"},
        "daily": [{"day": "2026-05-28", "count": 3, "bytes": 300, "bytes_h": "300 B", "pct": 100}],
        "participants": [{
            "patient": patient, "last_upload_time": "2026-05-28T20:00:00Z", "ago": "1h ago", "stale": False,
            "spark": [2, 3],
            "streams": [{"stream": "gps", "last_upload_time": "2026-05-28T20:00:00Z",
                         "ago": "1h ago", "stale": False, "last_size": 50, "last_size_h": "50 B"}],
        }],
        "feed": [{"patient": patient, "stream": "gps", "last_upload_time": "2026-05-28T20:00:00Z",
                  "ago": "1h ago", "last_size_h": "50 B"}],
        "stream_totals": [{"stream": "gps", "count": 3, "bytes": 300, "bytes_h": "300 B",
                           "last_upload_time": "2026-05-28T20:00:00Z", "ago": "1h ago", "stale": False}],
        "stale_hours": 24,
        "adherence": {
            "available": adherence_available, "days": ["2026-05-27", "2026-05-28"],
            "peak": 3, "omitted": 0,
            "rows": [{"patient": patient, "first_day": "2026-05-27", "cells": [
                {"day": "2026-05-27", "count": 0, "state": "zero"},
                {"day": "2026-05-28", "count": 3, "state": "data"},
            ]}],
        },
        "enrollment": {"total": 1, "points": [{"day": "2026-05-27", "cumulative": 1}]},
    }


class TestMetadataDashboard(ResearcherSessionTest):
    ENDPOINT_NAME = "metadata_dashboard_endpoints.metadata_dashboard_page"

    def test_happy_path_renders_views(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary()) as mock_ss:
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("Upload Activity Monitor", resp.content)
        self.assert_present("Recently active", resp.content)
        self.assert_present("Per-stream totals", resp.content)
        # the reader is called with the study's 24-char object_id, not the integer pk (R2)
        mock_ss.assert_called_once()
        self.assertEqual(mock_ss.call_args.args[0], self.session_study.object_id)

    def test_no_study_access_is_forbidden(self):
        # logged-in researcher with no relation to the study -> 403 (decorator enforced)
        self.smart_get_status_code(403, str(self.session_study.id))

    def test_nonexistent_study_404(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        self.smart_get_status_code(404, "0")

    def test_not_configured_state(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", side_effect=reader.MetadataIndexNotConfigured):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("not available in this environment", resp.content)

    def test_read_error_state_is_loud_with_no_data(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", side_effect=reader.MetadataIndexReadError):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("Could not reach the upload metadata index", resp.content)
        # the loud error must not be rendered alongside data tables
        self.assert_not_present("Recently active", resp.content)

    def test_invalid_study_maps_to_read_error(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", side_effect=reader.MetadataIndexInvalidStudy):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("Could not reach the upload metadata index", resp.content)

    def test_empty_state(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary(has_data=False)):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("No upload activity has been recorded", resp.content)

    def test_patient_id_is_html_escaped(self):
        # patient_id originates from an S3 key the server never sanitizes -> must be escaped
        # (appears in the feed, freshness, heatmap rows/cell-titles, and sparkline aria-label)
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary(patient="<script>alert(1)</script>")):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_not_present("<script>alert(1)", resp.content)
        self.assert_present("&lt;script&gt;alert(1)", resp.content)

    def test_adherence_heatmap_renders(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary()):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("Daily adherence", resp.content)
        self.assert_present("before first upload", resp.content)   # legend -> heatmap rendered
        self.assert_present("3 uploads", resp.content)             # a data cell's title

    def test_adherence_pre_backfill_state(self):
        # has_data true but the participant-daily aggregate is empty (backfill not run yet)
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary(adherence_available=False)):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("run the metadata-index backfill", resp.content)
        self.assert_not_present("before first upload", resp.content)  # the grid legend is not rendered

    def test_enrollment_renders(self):
        self.set_session_study_relation(ResearcherRole.researcher)
        with patch.object(reader, "study_summary", return_value=_summary()):
            resp = self.smart_get_status_code(200, str(self.session_study.id))
        self.assert_present("Enrollment", resp.content)
