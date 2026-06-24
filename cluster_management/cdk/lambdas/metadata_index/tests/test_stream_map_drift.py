"""Drift guard: the vendored stream map must stay identical to Beiwe's.

The Lambda vendors ``stream_map.DATA_STREAM_NAME_MAPPING`` because it runs without
the Django app. This test loads the REAL ``constants/data_stream_constants.py``
from the repo (directly by file path, so it needs no Django setup) and rebuilds
the superset exactly the way ``database/profiling_models.py:S3File`` does, then
asserts the vendored copy matches. If anyone edits the upload-token map or the
stream list in the repo, this fails the build instead of letting the Lambda
silently mis-parse in production.
"""
import importlib.util
import pathlib

import stream_map

# constants/data_stream_constants.py is 5 directories up from this test file:
# tests -> metadata_index -> lambdas -> cdk -> cluster_management -> <repo root>
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_CONSTANTS_PATH = _REPO_ROOT / "constants" / "data_stream_constants.py"


def _load_repo_constants():
    """Load the constants module directly by path (avoids importing the
    `constants` package __init__ and any Django side effects)."""
    spec = importlib.util.spec_from_file_location("_beiwe_data_stream_constants", _CONSTANTS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_constants_file_exists():
    assert _CONSTANTS_PATH.is_file(), f"expected repo constants at {_CONSTANTS_PATH}"


def test_vendored_map_matches_production_superset():
    c = _load_repo_constants()

    # Rebuild DATA_STREAM_NAME_MAPPING exactly as S3File does:
    expected = {**c.UPLOAD_FILE_TYPE_MAPPING}
    expected.update((stream, stream) for stream in c.ALL_DATA_STREAMS)
    expected["/keys/"] = "key_file"
    expected["forest"] = "forest"
    expected["ios/log"] = c.IOS_LOG_FILE

    assert stream_map.DATA_STREAM_NAME_MAPPING == expected


def test_ios_log_slash_form_present():
    # The single most important entry the naive UPLOAD_FILE_TYPE_MAPPING-only
    # check would miss: the slash form that real S3 keys actually contain.
    assert stream_map.DATA_STREAM_NAME_MAPPING.get("ios/log") == "ios_log"


def test_key_file_and_forest_present():
    assert stream_map.DATA_STREAM_NAME_MAPPING.get("/keys/") == "key_file"
    assert stream_map.DATA_STREAM_NAME_MAPPING.get("forest") == "forest"
