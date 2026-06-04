# Changes on `feat/ut-setup`

This document summarises all work done on this branch relative to `main`.

---

## 1. Automated cluster setup (`setup_beiwe.py`)

**Files:** `cluster_management/setup_beiwe.py`, `cluster_management/README.md`, `cluster_management/cdk/`

A new end-to-end setup script that provisions a complete Beiwe deployment on AWS from scratch. It orchestrates CDK (for VPC, RDS, EC2 key pair, and IAM), Elastic Beanstalk environment creation, RDS instance creation, and processing server launch — replacing the previously manual steps documented in the wiki.

Key features:
- Reads from an optional `general_configuration/setup_config.json` (see `setup_config.example.json`) to avoid prompting for values on re-runs.
- Always re-downloads the EC2 private key from AWS after CDK deployment, preventing SSH failures caused by a stale local copy.
- Passes the environment name to `launch_script.py` via `BEIWE_ENV_NAME` so Fabric retains TTY access during SSH-heavy subcommands.
- Supports Sentry DSN arguments with safe defaults.

**CDK stack (`cdk/prerequisites_stack.py`):** provisions a dedicated VPC, RDS PostgreSQL instance (in a DB subnet group scoped to that VPC), EC2 key pair, and an IAM user with S3 access. RDS is `PubliclyAccessible=False` and access-controlled by security groups; no NAT gateway is required because all EC2 instances are placed in public subnets.

Usage:
```bash
cd cluster_management/
python setup_beiwe.py
```

---

## 2. Deployment script bug fixes

**Files:** `cluster_management/deployment_helpers/aws/elastic_beanstalk.py`, `elastic_beanstalk_configuration.py`, `elastic_compute_cloud.py`, `rds.py`, `security_groups.py`, `configuration_utils.py`, `launch_script.py`

A series of fixes discovered while running `setup_beiwe.py` against a real AWS account for the first time:

- **VPC pinning:** EB environment, RDS, and processing servers now all land in the Beiwe VPC rather than the account-default VPC. Without this fix the three components ended up in different networks and could not reach each other.
- **Idempotency:** `create_eb_environment`, `create_new_rds_instance`, `create_processing_control_server`, `open_tcp_port`, and `create_finalized_configuration` are all now safe to re-run — they detect and skip already-existing resources rather than erroring out.
- **Security group handling:** VPC environments return security group IDs (not names); all relevant calls now handle both forms.
- **Custom Availability Zones EB option** removed — it is invalid when a VPC is specified and caused environment creation to fail.
- **`get_instances_by_name`** updated to include stopped instances (not just running ones), so `resume` can find and restart them.

---

## 3. Cluster cost management (`manage_beiwe.py`)

**File:** `cluster_management/manage_beiwe.py`

A new script to pause and resume the cluster's cost drivers (useful for non-production environments that don't need to run overnight).

**Pause** (in order):
1. Saves EB Auto Scaling Group min/max/desired to `environment_configuration/{env}_suspend_state.json`.
2. Sets ASG capacity to 0 (drains EB web instances).
3. Stops processing server EC2 instances.
4. Stops the RDS instance.

**Resume** (in order):
1. Starts RDS and waits until available.
2. Starts EC2 processing instances.
3. Restores ASG to saved min/max/desired (falls back to sane defaults if the state file is missing).

Usage:
```bash
cd cluster_management/
python manage_beiwe.py pause
python manage_beiwe.py resume
```

---

## 4. Scheduled nightly pause (`cdk/scheduler_stack.py`)

**Files:** `cluster_management/cdk/scheduler_stack.py`, `cdk/app.py`, `manage_beiwe.py`

A CDK stack (`BeiweSchedulerStack`) that runs the pause routine automatically every evening via EventBridge Scheduler + Lambda, so the cluster doesn't need to be manually paused each night.

- Uses `aws_scheduler.Schedule` (not the older `aws_events.Rule`) so the cron runs in **America/Chicago** time and handles CST/CDT transitions automatically — fires at 7:00 pm Central year-round with no DST rule-toggling.
- The Lambda mirrors the `manage_beiwe.py` pause logic and saves ASG state to SSM Parameter Store at `/beiwe/{env}/suspend_state` (in addition to the local JSON file).
- `manage_beiwe.py resume` falls back to SSM when the local state file is absent, so a morning-after manual resume works correctly after an overnight Lambda-triggered pause.

Deploy once with:
```bash
cd cluster_management/cdk/
cdk deploy BeiweSchedulerStack --context region=us-east-1
```

---

## 5. Security audit notes (`NOTES.md`)

**File:** `cluster_management/NOTES.md`

Documents security findings surfaced during setup and review, including a note that the sysadmin email address is visible to unauthenticated users on the Beiwe login page.

---

## 6. Zenodo data import command (`import_beiwe_data`)

**File:** `database/management/commands/import_beiwe_data.py`

A Django management command to import pre-processed Beiwe CSV data (e.g. from the [Zenodo public dataset](https://zenodo.org/records/6471045)) into a live Beiwe study instance.

The source directory must be structured as the Beiwe data download pipeline produces:
```
<data_dir>/
    <patient_id>/
        <data_stream>/
            <YYYY-MM-DD HH_MM_SS+00_00>.csv
```

What the command does:
- Creates `Participant` records for each patient directory found (with a random throwaway password), inferring iOS vs. Android from the presence of `ios_log` vs. `app_log`/`calls`/`texts` directories.
- For each CSV: computes SHA1 (for `S3File`) and MD5-base64 (for `ChunkRegistry.chunk_hash`), compresses with Zstandard, encrypts with the study's AES key, and uploads to `CHUNKED_DATA/<study_id>/<patient_id>/<stream>/<timestamp>.csv` in S3.
- Registers each file in `ChunkRegistry` (chunkable streams) or via `register_unchunked_data` (survey answers, audio).
- After all files for a participant are processed, queries `ChunkRegistry` for the actual time range and calls `calculate_data_quantity_stats` to populate `SummaryStatisticDaily` — required for data to appear in the Beiwe web UI.
- Skips files already present in `ChunkRegistry`, so re-runs are safe.
- `survey_timings` and `survey_answers` are imported with `survey_id=None` (nullable FK) since the source data does not include survey identifiers.

Usage:
```bash
# Preview — no data written
python manage.py import_beiwe_data "Study Name" /path/to/data --dry-run

# Real import
python manage.py import_beiwe_data "Study Name" /path/to/data

# Identify study by 24-character object_id instead of name
python manage.py import_beiwe_data <object_id> /path/to/data
```
