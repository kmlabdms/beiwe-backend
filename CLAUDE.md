# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development environment

Local development runs entirely via Docker Compose. Copy `docker_management/.envs/.env.dev.template` to `docker_management/.envs/.env.dev` and fill in values before first run.

```bash
make dev-up-build-detach   # build images, run containers, migrate, collect static (detached)
make dev-migrate            # run migrations inside the running web container
make dev-collect-static     # collect static files inside the running web container
```

Scripts and management commands that need DB or S3 access must be run on a server (EC2 processing instance) or via the Docker web container — not directly from a developer laptop, because RDS is `PubliclyAccessible=False` inside a private VPC.

To SSH into a processing instance (Ubuntu AMI, user is `ubuntu`, not `ec2-user`):
```bash
ssh -i ~/.ssh/beiwe-deployment-key.pem ubuntu@<instance-ip>
```

## Running tests

On macOS, `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required. Tests use Django's test runner against a real PostgreSQL database (not SQLite).

```bash
# Run all tests
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests/ --parallel

# Run a single test module
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests.test_file_processing

# Run a single test class or method
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests.test_file_processing.TestCsvMerger
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests.test_file_processing.TestCsvMerger.test_construct_s3_chunk_path

# Reuse the test database across runs (much faster)
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py test tests/ --keepdb --parallel
```

Django settings module: `config.django_settings`. `RUNNING_TESTS` is set to `True` automatically when `"test"` is in `sys.argv`, which disables SSL redirects and adjusts other settings.

## Linting and formatting

There is no enforced formatter. `isort` and `ruff` are configured in `pyproject.toml` but not required to pass. The primary dev uses `flake8` for a subset of errors (also configured in `pyproject.toml`). `mypy` is used for type checking but not enforced.

## Architecture overview

Beiwe is a Django application for collecting smartphone sensor and survey data in research studies. The backend has two distinct runtime contexts that share the same codebase:

**Web servers (Elastic Beanstalk)** handle researcher-facing web UI, participant registration, and mobile app data upload endpoints.

**Data processing servers (EC2)** run Celery workers that consume from a RabbitMQ broker. They pick up `FileToProcess` queue entries every 6 minutes and run the chunking pipeline.

### Data lifecycle

1. **Mobile upload** → `endpoints/mobile_endpoints.py` decrypts the file (RSA + AES), writes it to S3 at `<study_object_id>/<patient_id>/<data_stream>/<timestamp>`, and creates a `FileToProcess` DB entry.

2. **Celery processing** → `services/celery_data_processing.py` queues one task per participant with pending files. `libs/file_processing/file_processing_core.py` (`FileProcessingTracker`) downloads each file, parses and bins it into hourly chunks (chunkable streams) or registers it as-is (unchunkable streams), then uploads to `CHUNKED_DATA/<study_object_id>/<patient_id>/<data_stream>/<timestamp>.csv`.

3. **ChunkRegistry** → every processed file gets a `ChunkRegistry` row (in `database/data_access_models.py`) with `chunk_path`, `chunk_hash` (MD5, base64-encoded), `data_type`, `time_bin`, and `file_size`. This is the source of truth for the data download API.

4. **SummaryStatisticDaily** → after processing, `libs/file_processing/data_qty_stats.calculate_data_quantity_stats()` writes per-day per-stream byte counts to `SummaryStatisticDaily` (in `database/forest_models.py`). This is what drives the dashboard/UI data display. If you write to ChunkRegistry directly (e.g. via a data import script), you must call this function manually or the UI will show no data.

### S3 storage layout

All files on S3 are Zstandard-compressed and AES-encrypted with the study's `encryption_key`. The `libs/s3.py:S3Storage` class manages this lifecycle — always use it rather than calling boto3 directly.

```
<study_object_id>/<patient_id>/<data_stream>/<timestamp>       # raw uploads
CHUNKED_DATA/<study_object_id>/<patient_id>/<data_stream>/<timestamp>.csv  # processed chunks
PROBLEM_UPLOADS/<study_object_id>/<patient_id>/...             # decryption failures
```

Files are stored with a `.zst` extension on S3. S3 paths passed to `S3Storage` should never include `.zst` — the class appends it.

Two hashes are in use and must not be confused:
- `S3File.sha1` — raw SHA1 bytes (20 bytes), stored as `BinaryField`, used for deduplication in S3.
- `ChunkRegistry.chunk_hash` — MD5 base64-encoded string (24 chars), computed via `libs/utils/security_utils.chunk_hash()`.

### Chunkable vs. unchunkable data streams

Defined in `constants/data_stream_constants.py`. **Chunkable** streams (GPS, accelerometer, gyro, etc.) are binned into hourly files and merged across uploads covering the same hour. **Unchunkable** streams (survey answers, audio recordings) are stored as individual files. `ChunkRegistry.survey` FK is nullable — survey data imported without a survey context should use `survey_id=None`.

### Key memory warnings

`ChunkRegistry` and `UploadTracking` are very large tables on production. Always query them with `.iterator()` and `.values_list()` — never load full model instances in bulk. The codebase has comments throughout reinforcing this.

### Large queries and S3File

`S3File` (`database/profiling_models.py`) is a registry of every file ever written to S3. It is updated automatically by `S3Storage.update_s3_table()` after each upload. It tracks `sha1`, `size_compressed`, `size_uncompressed`, and whether the file has been archived to Glacier.

## Management commands

```bash
# Import pre-processed Beiwe CSV data (e.g. from a public Zenodo dataset) into a study.
# Source directory must be structured as <data_dir>/<patient_id>/<data_stream>/<timestamp>.csv
python manage.py import_beiwe_data "Study Name" /path/to/data
python manage.py import_beiwe_data "Study Name" /path/to/data --dry-run

# Study can also be identified by its 24-character object_id
python manage.py import_beiwe_data <object_id> /path/to/data
```

## Deployment

Deployment uses Elastic Beanstalk for web servers and a custom launch script (`cluster_management/launch_script.py`) for processing servers. The EB environment is named `kowalski-beiwe`, application `beiwe-application`, region `us-east-1`, profile `eb-cli`.

Data processing servers are standalone EC2 Ubuntu instances, not part of the EB environment. They are managed via `cluster_management/manage_beiwe.py`.

### Upload Metadata Index (additive, opt-in)

`cluster_management/cdk/MetadataIndexStack` is an additive, event-driven layer that records lightweight metadata about each raw S3 upload (S3 → EventBridge → SQS → Lambda → DynamoDB) for upload-activity monitoring. It does **not** change the upload path, read/decrypt object contents, or touch Postgres; raw S3 stays the system of record. It is opt-in (`cdk deploy MetadataIndexStack -c enable_metadata_index=true`) and safe to disable (delete the EventBridge rule) or tear down (`cdk destroy`). Its Lambda tests run via the CDK venv with `pytest`+`moto`, **not** the Django test runner. See `cluster_management/cdk/METADATA_INDEX.md`.

The web app exposes an opt-in, read-only per-study **Upload Activity** dashboard over this index (`endpoints/metadata_dashboard_endpoints.py` + `libs/metadata_index_reader.py`, gated by `METADATA_INDEX_ENABLED` and the `METADATA_INDEX_*` settings). It reads DynamoDB per study with `Query`/`begins_with` only (never `Scan`) by assuming the least-privilege `MetadataIndexReaderRole` via STS; the web IAM principal must be granted `sts:AssumeRole` on that role. Reuses `@authenticate_researcher_study_access` for per-study scoping. See the "Reader access" section of `cluster_management/cdk/METADATA_INDEX.md`.
