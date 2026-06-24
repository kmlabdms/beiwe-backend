# Beiwe Hosting Cost — Summary

**Prepared for study-design and grant-budgeting purposes.**
Scope: AWS infrastructure to **collect, store, and query** Beiwe data. It does **not**
include personnel, participant devices/incentives, or downstream analysis compute.
Figures are a *dynamic estimate* — AWS list prices in us-east-1 as of the worksheet
build date — not a fixed quote. The accompanying `beiwe_cost_worksheet.xlsx` lets you
change any assumption and recalculate.

---

## Suggested grant-application language

*Drop-in prose for a grant budget justification. Numbers are grounded in the worksheet
and should be revisited as AWS pricing and study parameters change.*

> **Cloud hosting costs.** Data collection and management for this study will be hosted on
> Amazon Web Services (AWS). For a study of approximately 20–25 participants, each monitored
> for up to 90 days, cloud infrastructure is estimated at **approximately $220 per month —
> roughly $660 over the 90-day collection period, or about $26 per participant**. This
> estimate covers the AWS resources required to collect, store, and query participant data —
> the application servers, database, and data storage — and does not include personnel,
> participant devices or incentives, or downstream data analysis. Most of this cost is the
> continuously running server and database infrastructure, which is largely fixed regardless
> of enrollment; the incremental cost of each additional participant's data is small, on the
> order of a few dollars per participant per month.
>
> Because cloud pricing and resource usage change over time, this figure is a **dynamic
> estimate** based on current AWS pricing (US East region) and on data volumes observed in
> comparable public Beiwe datasets, rather than a fixed quote. Costs scale efficiently with
> study size: the same architecture supports substantially larger studies through modest,
> stepwise increases in compute and database capacity, and the per-participant cost falls as
> enrollment grows. After a study concludes, long-term data-retention costs can be reduced
> further by archiving inactive data to low-cost cold storage.

**One-line budget-justification variant:**

> Cloud data hosting (AWS): ~$220/month (~$660 for a 90-day, ~25-participant study) for
> collection, storage, and query of participant data; excludes personnel and analysis.
> Dynamic estimate at current AWS pricing.

---

## Bottom line

For the planned study — **~20–25 participants, up to 90 days of monitoring each** — running
Beiwe on AWS costs approximately:

> **≈ $660 total for the 90-day collection period (≈ $220 / month), or about $26 per participant.**

The cost is driven almost entirely by the **always-on servers**, not by the amount of
participant data. At this scale the per-participant data cost is only about **$3 / month
for all 25 participants combined** (≈ $0.13 per participant per month).

| | Monthly | 90-day study |
|---|---:|---:|
| Fixed infrastructure (servers, database, load balancer) | **$217** | $650 |
| Per-participant data (storage, requests, monitoring index) | **$3** | $10 |
| **Total** | **$220** | **≈ $660** |

---

## What you're paying for

**Fixed infrastructure (~$217/mo, ~98% of the cost at this scale).** These run 24/7
regardless of how many participants are enrolled:

- **Database (Amazon RDS PostgreSQL) — ~$125/mo.** The single largest line; it's the
  system of record for the data index and study metadata.
- **Web server (researcher UI + the app's upload endpoint) — ~$30/mo.**
- **Processing server (chunks and files incoming data; also runs the message broker) — ~$30/mo.**
- **Load balancer — ~$18/mo**, plus a small amount of storage, secrets, and monitoring overhead.

**Per-participant data (~$3/mo for all 25).** This scales with enrollment but is small:

- **S3 storage** of the sensor data (compressed). Anchored to real sample datasets:
  ~13 MiB per participant per day, ~0.4 GB/participant/month. *Heavily dependent on which
  sensors are enabled* — motion sensors (accelerometer/gyroscope/magnetometer) were 95% of
  the sample data; a study without them would store far less.
- **Upload-monitoring index** (the new DynamoDB + Lambda layer that powers the data dashboard):
  serverless and usage-based — **pennies per month**, as expected.

---

## Scaling: this study fits comfortably; larger studies step up

The current single-server setup handles roughly **150 participants** before it needs more
processing power. Beyond that, processing workers and a larger database are added in steps.
The worksheet's **Tiers** sheet models this; a few reference points (same 90-day study shape,
more participants):

| Participants | Servers | Database | **Total / month** | $ / participant / month |
|---:|---|---|---:|---:|
| **25** (this grant) | 1 web + 1 processor | db.m5.large | **$220** | $8.80 |
| 100 | 1 web + 1 processor | db.m5.large | $230 | $2.30 |
| 500 | + 2 workers | db.m5.xlarge | $550 | $1.10 |
| 1,000 | + 4 workers | db.m5.xlarge | $760 | $0.76 |
| 5,000 | + 20 workers | db.m5.4xlarge | $3,225 | $0.65 |
| 25,000 | + 100 workers | db.m5.4xlarge | $11,900 | $0.48 |

The **per-participant cost falls sharply with scale** as the fixed servers are amortized
across more participants — useful when budgeting multi-study or larger efforts. (The scaling
ratios are conservative rules of thumb; they should be calibrated against live usage before
quoting very large studies.)

---

## Ways to reduce the cost

- **Compute Savings Plans / Reserved Instances:** committing to 1 or 3 years cuts the
  server and database cost by roughly **40–50%**. Because fixed servers dominate at this
  scale, this is the highest-impact lever for an ongoing platform.
- **Pause when idle:** a nightly-pause scheduler (already built) stops the servers
  overnight for non-production environments; after a study finishes collecting, the servers
  can be paused or downsized entirely.
- **Archive cold data:** long-term retention after a study ends can move to S3 Glacier Deep
  Archive (~25× cheaper than standard storage). Retaining a finished 25-participant study is
  only a few dollars per month — or cents in Glacier.

---

## Notes for budgeting

- These are **AWS costs only** — no personnel, devices, or analysis compute.
- Costs **fluctuate** with AWS pricing and with the sensors a study enables; treat them as
  approximate and revisit periodically.
- If the infrastructure is **shared across multiple concurrent studies**, the fixed cost is
  spread among them and the marginal cost of an additional study is closer to the
  per-participant figure (a few dollars per month).
- All numbers are reproducible and adjustable in `beiwe_cost_worksheet.xlsx` (see the Inputs,
  Scaling, and Tiers sheets).
