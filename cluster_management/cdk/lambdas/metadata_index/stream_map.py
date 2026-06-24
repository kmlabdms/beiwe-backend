"""Vendored copy of Beiwe's S3-key -> data-stream mapping.

The Lambda runs without the Django app, so it cannot import
``database.profiling_models.S3File.DATA_STREAM_NAME_MAPPING`` (which needs Django
configured) or even ``constants.data_stream_constants`` (not on the deployed
Lambda's path). This module reproduces that mapping exactly.

The single source of truth in the repo is the production parser
(``libs/file_processing/utility_functions_simple.py:s3_file_path_to_data_type``)
and ``S3File.DATA_STREAM_NAME_MAPPING``, which is built as::

    {**UPLOAD_FILE_TYPE_MAPPING}                       # mobile token -> canonical
    + {stream: stream for stream in ALL_DATA_STREAMS}  # canonical self-maps
    + {"/keys/": "key_file", "forest": "forest", "ios/log": IOS_LOG_FILE}

``tests/test_stream_map_drift.py`` imports the real ``constants.data_stream_constants``
and asserts this vendored copy still matches, so any drift in the repo fails the
build rather than silently mis-parsing in production.
"""

# --- vendored from constants/data_stream_constants.py -----------------------

# mobile-side upload tokens -> canonical stream name (the strings used in chunked
# files and the dashboard). These are the tokens that actually appear as a path
# segment in raw S3 keys.
_UPLOAD_FILE_TYPE_MAPPING = {
    "accel": "accelerometer",
    "voiceRecording": "audio_recordings",
    "bluetoothLog": "bluetooth",
    "callLog": "calls",
    "devicemotion": "devicemotion",
    "gps": "gps",
    "gyro": "gyro",
    "logFile": "app_log",
    "magnetometer": "magnetometer",
    "powerState": "power_state",
    "reachability": "reachability",
    "surveyAnswers": "survey_answers",
    "surveyTimings": "survey_timings",
    "textsLog": "texts",
    "wifiLog": "wifi",
    "proximity": "proximity",
    "ios_log": "ios_log",
    "ambientAudio": "ambient_audio",
    "identifiers": "identifiers",
}

# every canonical stream name (used in chunked file paths) maps to itself
_ALL_DATA_STREAMS = [
    "accelerometer",
    "audio_recordings",
    "ambient_audio",
    "app_log",
    "bluetooth",
    "calls",
    "devicemotion",
    "gps",
    "gyro",
    "identifiers",
    "ios_log",
    "magnetometer",
    "power_state",
    "proximity",
    "reachability",
    "survey_answers",
    "survey_timings",
    "texts",
    "wifi",
]

IOS_LOG_FILE = "ios_log"
IDENTIFIERS = "identifiers"

# Streams that are NOT participant data uploads. They are recognized so they can
# be classified as Ignore rather than Malformed (avoids false Malformed alarms).
NON_UPLOAD_STREAMS = frozenset({"key_file", "forest"})

# The superset map, built exactly like S3File.DATA_STREAM_NAME_MAPPING.
DATA_STREAM_NAME_MAPPING = {**_UPLOAD_FILE_TYPE_MAPPING}
DATA_STREAM_NAME_MAPPING.update((stream, stream) for stream in _ALL_DATA_STREAMS)
DATA_STREAM_NAME_MAPPING["/keys/"] = "key_file"
DATA_STREAM_NAME_MAPPING["forest"] = "forest"
DATA_STREAM_NAME_MAPPING["ios/log"] = IOS_LOG_FILE
