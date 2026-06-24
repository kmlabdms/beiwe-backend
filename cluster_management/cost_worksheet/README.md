# Beiwe AWS Cost Worksheet

`beiwe_cost_worksheet.xlsx` is a live, editable model of the monthly AWS cost of a
Beiwe deployment. It separates **fixed / fixed-ish costs** (servers, load balancer,
database) from **per-patient costs** (storage, requests, the metadata index), as
requested.

## Scenario modelled

A **24/7 always-on production** deployment in **us-east-1**, including the additive
infrastructure introduced on the `feat/ut-setup` branch:

- the **DynamoDB upload-metadata index** (S3 → EventBridge → SQS → Lambda → DynamoDB), and
- the **dedicated VPC with no NAT gateway** (saves ~$35/mo vs. a private-subnet design).

The branch's **nightly-pause scheduler** is *not* applied in the base model — it would
reduce the compute lines. It is listed on the Fixed Costs sheet as an optional saving.

The model is **threshold-aware**: Beiwe's compute and database step up as enrollment grows,
so worker count, RDS storage, and RDS instance class are *derived* from the patient count
via tunable ratios on the **Scaling** sheet rather than being flat. The **Tiers** sheet
recomputes the whole stack at several patient counts so the step-ups are visible.

The **default Inputs scenario** is the planned grant: **~25 patients, 90 days (3 months)** of
monitoring. `COST_WRITEUP.md` is the brief, grant-oriented summary built from this worksheet.

## How to use

1. Open in Excel, Google Sheets, or LibreOffice so the formulas recalculate.
2. Edit the **yellow cells** on the **Inputs** sheet (patient count, study length,
   data per patient, worker count, etc.).
3. Unit rates live on the **Pricing** sheet (also yellow/editable) — update if AWS
   prices change.
4. Read the bottom line on the **Totals** sheet.

## Sheets

| Sheet | Contents |
|---|---|
| Overview | Scenario, instructions, key assumptions |
| Inputs | Editable assumptions — everything else references these |
| Scaling | Tunable threshold ratios (patients/worker, DB growth, RDS class breakpoints), derived capacity, and documentation of where each step-function is |
| Savings Plan | Dropdown picker for a Savings Plan / Reserved Instance term; discounts the compute (EC2 + RDS) lines and shows the monthly/annual savings |
| Fixed Costs | EB web tier, Classic LB, processing manager (+RabbitMQ), workers, RDS, EBS, overhead — capacity derived from patient count |
| Per-Patient Costs | S3 storage (raw+chunked), S3 requests, DynamoDB metadata index, egress (genuinely linear) |
| Totals | Fixed vs per-patient split, monthly/annual, $/patient/month (current Inputs scenario) |
| Tiers | Whole stack recomputed at 100 / 500 / 1k / 5k / 25k patients — shows the step-ups |
| Pricing | us-east-1 on-demand unit rates, with sources |
| Sample Data | The measured storage anchor (see below) |

## Scaling thresholds

Beiwe's per-patient costs (S3, the metadata index) are linear, but its **compute and
database step up** with enrollment. The Scaling sheet models this; defaults are
conservative rules of thumb flagged for production calibration.

| Component | What pushes it | Action at threshold |
|---|---|---|
| **Processing workers** | Files per 6-min cycle (tasks expire at 5:30). **Not auto-scaled** — added by hand. | Add `m5.large` workers |
| **RDS storage** | `ChunkRegistry` + `UploadTracking` row growth | Raise allocation; maybe gp3/io1 for IOPS |
| **RDS instance class** | DB connections + CPU (web + every celery process) | Step large → xlarge → 2xlarge → 4xlarge |
| **Web tier** | Mobile upload request rate (ASG Max=2 today) | Raise MaxSize / instance size |
| S3 / DynamoDB / CLB | — | Nothing (genuinely linear / auto-scaling) |

## Storage anchor (how the per-patient number was derived)

Per-patient storage is anchored to a real measurement of the sample datasets:

- **Source:** `s3://beiwe-data-kowalski-beiwe-rxjfjw9miockp1gzxa42gd2zikzdrd3ndrclz`,
  prefix `CHUNKED_DATA/2grtzwKjSxi64uYkxqZASgxe/`
- **Measured 2026-06-24** with `aws s3 ls --recursive` (sizes are the compressed `.zst`
  bytes — exactly the S3 billing basis).
- **8 patients, 7,107 objects, 1.117 GB** total → ~133 MiB/patient over ~8–22 day active
  spans → **~13 MiB/patient/day** (the `0.381 GB/patient/month` default input).
- Sample is **chunked (processed) data only**. Raw uploads are added via the
  **raw-upload retention multiplier** input (default 1.0× chunked → total ≈ 2× chunked).

**Important caveat:** the sample is **95% motion sensors** (accelerometer 55%, gyro 26%,
magnetometer 15%). A study that does *not* collect high-frequency motion data will store
far less, so confirm the actual enabled streams before quoting storage.

## Results by tier (90-day study, default streams, conservative scaling ratios)

| Patients | Workers | RDS class | Fixed/mo | Per-patient/mo | **Total/mo** | All-in $/patient/mo |
|---:|---:|---|---:|---:|---:|---:|
| **25** (grant) | 0 | db.m5.large | $217 | $3 | **$220** | $8.80 |
| 100 | 0 | db.m5.large | $217 | $13 | **$230** | $2.30 |
| 500 | 2 | db.m5.xlarge | $485 | $66 | **$550** | $1.10 |
| 1,000 | 4 | db.m5.xlarge | $628 | $132 | **$760** | $0.76 |
| 5,000 | 20 | db.m5.4xlarge | $2,565 | $659 | **$3,225** | $0.65 |
| 25,000 | 100 | db.m5.4xlarge | $8,616 | $3,298 | **$11,913** | $0.48 |

At the grant scale the **RDS `db.m5.large` instance (~$125/mo)** dominates and the data
itself is only ~$3/mo. As enrollment grows, fixed cost amortizes (all-in $/patient falls from
$8.80 → $0.48) but the **worker tier and RDS class step up** — the worker count is the sharpest
driver at scale and is the conservative ratio most worth calibrating against production telemetry.

A **90-day study of ~25 patients ≈ $660 total** (3 months × ~$220/mo).

## Regenerating

```bash
python build_cost_worksheet.py        # rewrites beiwe_cost_worksheet.xlsx
```

The generator (`build_cost_worksheet.py`) writes live Excel formulas; it does not bake in
computed values, so the workbook stays editable. The numbers above were verified by
recalculating the workbook headlessly in LibreOffice and cross-checking against an
independent Python reproduction of the model.
