#!/usr/bin/env python3
"""Generate the Beiwe AWS cost worksheet (beiwe_cost_worksheet.xlsx).

Scenario modelled: a 24/7 always-on production Beiwe deployment in us-east-1,
including the additive infrastructure introduced on the feat/ut-setup branch
(the DynamoDB upload-metadata index, and the no-NAT dedicated-VPC design).

The workbook is THRESHOLD-AWARE: worker count, RDS storage, and RDS instance
class are derived from the patient count via tunable ratios on the Scaling sheet,
so the "fixed" costs step up as enrollment grows rather than staying flat. A Tiers
sheet recomputes the whole stack at several patient counts so the step-ups are
visible. Every number is a live Excel formula referencing the editable Inputs,
Scaling, and Pricing sheets.

Storage is anchored to a real measurement of the sample datasets in
  s3://beiwe-data-kowalski-beiwe-rxjfjw9miockp1gzxa42gd2zikzdrd3ndrclz
    /CHUNKED_DATA/2grtzwKjSxi64uYkxqZASgxe
(8 patients, 1.117 GB compressed, ~13 MiB/patient/day) — see the Sample Data sheet.

Run:
    python build_cost_worksheet.py
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------
TITLE_FONT = Font(bold=True, size=16, color="1F3864")
H2_FONT = Font(bold=True, size=12, color="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")      # yellow = editable
DERIVED_FILL = PatternFill("solid", fgColor="E2EFDA")    # green = computed
SUBTOTAL_FILL = PatternFill("solid", fgColor="DDEBF7")
TOTAL_FILL = PatternFill("solid", fgColor="FCE4D6")
SECTION_FILL = PatternFill("solid", fgColor="D9E1F2")
NOTE_FONT = Font(italic=True, size=9, color="666666")
BOLD = Font(bold=True)
MONEY = '"$"#,##0.00'
MONEY0 = '"$"#,##0'
NUM = "#,##0"
NUM2 = "#,##0.00"
PCT = "0%"
thin = Side(style="thin", color="BBBBBB")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
WRAP = Alignment(wrap_text=True, vertical="top")


def style_header(ws, row, last_col):
    for c in range(1, last_col + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = BORDER


def set_widths(ws, widths):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


# ===========================================================================
wb = Workbook()

# ---------------------------------------------------------------------------
# PRICING sheet
# ---------------------------------------------------------------------------
pr = wb.active
pr.title = "Pricing"
pr["A1"] = "AWS Unit Pricing — us-east-1 (N. Virginia)"
pr["A1"].font = TITLE_FONT
pr["A2"] = ("On-demand list prices. Verify against the AWS pricing pages before quoting; "
            "rates current as of the build date below. Changing a rate here updates every "
            "dependent formula in the workbook.")
pr["A2"].font = NOTE_FONT
pr["A2"].alignment = WRAP
pr.merge_cells("A2:E2")
pr.row_dimensions[2].height = 30

PR_HEADER = 4
for i, label in enumerate(["Item", "Unit", "Rate (USD)", "Source / notes"]):
    pr.cell(row=PR_HEADER, column=i + 1, value=label)
style_header(pr, PR_HEADER, 4)

PRICES = [
    ("ec2_t3_medium", "EC2 t3.medium (Linux, on-demand)", "$/hour", 0.0416, "EC2 on-demand"),
    ("ec2_t3_large", "EC2 t3.large (Linux, on-demand)", "$/hour", 0.0832, "EC2 on-demand"),
    ("ec2_m5_large", "EC2 m5.large (Linux, on-demand)", "$/hour", 0.096, "EC2 on-demand"),
    ("rds_m5_large", "RDS db.m5.large (PostgreSQL, Single-AZ)", "$/hour", 0.171, "RDS PostgreSQL on-demand"),
    ("rds_m5_xlarge", "RDS db.m5.xlarge (PostgreSQL, Single-AZ)", "$/hour", 0.342, "RDS PostgreSQL on-demand"),
    ("rds_m5_2xlarge", "RDS db.m5.2xlarge (PostgreSQL, Single-AZ)", "$/hour", 0.684, "RDS PostgreSQL on-demand"),
    ("rds_m5_4xlarge", "RDS db.m5.4xlarge (PostgreSQL, Single-AZ)", "$/hour", 1.368, "RDS PostgreSQL on-demand"),
    ("rds_gp2", "RDS gp2 storage", "$/GB-month", 0.115, "RDS storage"),
    ("rds_backup", "RDS backup storage (beyond free allocation)", "$/GB-month", 0.095, "Free up to 100% of provisioned storage"),
    ("clb_hour", "Classic Load Balancer (hourly)", "$/hour", 0.025, "ELB Classic (~$18/mo)"),
    ("clb_gb", "Classic LB data processed", "$/GB", 0.008, "ELB Classic"),
    ("ebs_gp3", "EBS gp3 volume", "$/GB-month", 0.08, "EBS"),
    ("s3_standard", "S3 Standard storage", "$/GB-month", 0.023, "First 50 TB"),
    ("s3_glacier_da", "S3 Glacier Deep Archive storage", "$/GB-month", 0.00099, "Cold archive tier"),
    ("s3_put", "S3 PUT/POST/LIST requests", "$/1,000 requests", 0.005, "S3 request pricing"),
    ("s3_get", "S3 GET requests", "$/1,000 requests", 0.0004, "S3 request pricing"),
    ("ddb_wru", "DynamoDB write request units (on-demand)", "$/million", 1.25, "DynamoDB on-demand"),
    ("ddb_rru", "DynamoDB read request units (on-demand)", "$/million", 0.25, "DynamoDB on-demand"),
    ("ddb_storage", "DynamoDB storage", "$/GB-month", 0.25, "DynamoDB"),
    ("lambda_req", "Lambda requests", "$/million", 0.20, "Lambda"),
    ("lambda_gbsec", "Lambda compute", "$/GB-second", 0.0000166667, "Lambda"),
    ("sqs_req", "SQS requests", "$/million", 0.40, "Standard queue; first 1M/mo free"),
    ("dto", "Data transfer out to internet", "$/GB", 0.09, "First 100 GB/mo free; tiers down past 10 TB"),
    ("secrets", "Secrets Manager secret", "$/secret-month", 0.40, "Secrets Manager"),
    ("route53", "Route 53 hosted zone", "$/month", 0.50, "First 25 hosted zones"),
    ("cloudwatch", "CloudWatch logs + metrics (estimate)", "$/month", 3.00, "Rough fixed allowance"),
]
P = {}
r = PR_HEADER + 1
for key, label, unit, rate, source in PRICES:
    pr.cell(row=r, column=1, value=label).border = BORDER
    pr.cell(row=r, column=2, value=unit).border = BORDER
    c = pr.cell(row=r, column=3, value=rate)
    c.number_format = '"$"#,##0.00000000'
    c.border = BORDER
    c.fill = INPUT_FILL
    pr.cell(row=r, column=4, value=source).border = BORDER
    P[key] = r
    r += 1
pr.cell(row=r + 1, column=1, value="Build date").font = BOLD
pr.cell(row=r + 1, column=2, value="2026-06-24")
set_widths(pr, {"A": 44, "B": 18, "C": 16, "D": 40})


def price(key):
    return f"Pricing!$C${P[key]}"


# ---------------------------------------------------------------------------
# INPUTS sheet
# ---------------------------------------------------------------------------
inp = wb.create_sheet("Inputs")
inp["A1"] = "Inputs — edit the yellow cells"
inp["A1"].font = TITLE_FONT
inp["A2"] = ("Server capacity (workers, RDS size/class, web instances) is DERIVED from the "
             "patient count on the Scaling sheet — adjust the ratios there. Everything else "
             "recalculates from these values and the Pricing sheet.")
inp["A2"].font = NOTE_FONT
inp["A2"].alignment = WRAP
inp.merge_cells("A2:D2")
inp.row_dimensions[2].height = 30

IN_HEADER = 4
for i, label in enumerate(["Input", "Value", "Unit", "Notes"]):
    inp.cell(row=IN_HEADER, column=i + 1, value=label)
style_header(inp, IN_HEADER, 4)

INPUTS = [
    ("n_patients", "Number of enrolled patients", 25, "patients",
     "Default = the planned grant scenario (~20-25 patients). Drives derived server capacity (Scaling sheet).", NUM),
    ("months_collection", "Months of data collection per patient", 3, "months",
     "Default = 90 days monitoring (the grant scenario). Storage accumulates over this period and is retained afterward.", NUM),
    ("chunked_gb_pt_mo", "Chunked data generated per patient / month", 0.381, "GB/patient/month",
     "Sample anchor: ~13 MiB/day (see Sample Data). HIGHLY dependent on enabled streams — "
     "motion sensors (accel/gyro/magnetometer) were 95% of the sample.", NUM2),
    ("raw_multiplier", "Raw-upload retention multiplier", 1.0, "x chunked",
     "Raw uploads are also kept on S3 (~1.0x the chunked volume; total ~2x chunked). "
     "Set to 0 if raw is deleted after processing.", NUM2),
    ("glacier_fraction", "Fraction archived to Glacier Deep Archive", 0.0, "0-1",
     "Cold data can be archived (S3File tracks Glacier state). 0 = everything in S3 Standard.", PCT),
    ("ebs_gb_per_instance", "EBS root volume per server", 20, "GB",
     "Applied to web + 1 manager + worker instances.", NUM),
    ("uploads_pt_day", "Raw uploads per patient / day", 150, "uploads/day",
     "Drives metadata-index, S3-request, and DB-growth costs. Estimate; small either way.", NUM),
    ("egress_gb_mo", "Data egress (researcher downloads) / month", 0, "GB/month",
     "Participant data downloaded over the internet. First 100 GB/mo is free.", NUM),
    ("hours_month", "Hours per month", 730, "hours",
     "Billing convention (365*24/12). Leave at 730 for always-on.", NUM),
    ("days_month", "Days per month", 30.44, "days",
     "365.25/12. Converts per-day rates to per-month.", NUM2),
    ("ddb_writes_per_upload", "DynamoDB writes per upload", 5, "writes",
     "Metadata-index Lambda: marker + per-stream rollup + study rollup + 2 latest-pointers.", NUM),
]
I = {}
r = IN_HEADER + 1
for key, label, value, unit, note, fmt in INPUTS:
    inp.cell(row=r, column=1, value=label).border = BORDER
    c = inp.cell(row=r, column=2, value=value)
    c.fill = INPUT_FILL
    c.border = BORDER
    c.number_format = fmt
    inp.cell(row=r, column=3, value=unit).border = BORDER
    n = inp.cell(row=r, column=4, value=note)
    n.alignment = WRAP
    n.font = NOTE_FONT
    n.border = BORDER
    inp.row_dimensions[r].height = 30
    I[key] = r
    r += 1
set_widths(inp, {"A": 38, "B": 12, "C": 16, "D": 60})


def inv(key):
    return f"Inputs!$B${I[key]}"


# ---------------------------------------------------------------------------
# SCALING sheet  (tunable threshold ratios + derived capacity + documentation)
# ---------------------------------------------------------------------------
sc = wb.create_sheet("Scaling")
sc["A1"] = "Scaling & thresholds"
sc["A1"].font = TITLE_FONT
sc["A2"] = ("Beiwe's compute and database step up as enrollment grows. These ratios convert "
            "patient count into required capacity. Defaults are CONSERVATIVE rules of thumb — "
            "calibrate the flagged ones against production telemetry (queue lag, DB size, CPU).")
sc["A2"].font = NOTE_FONT
sc["A2"].alignment = WRAP
sc.merge_cells("A2:D2")
sc.row_dimensions[2].height = 30

SC_HEADER = 4
for i, label in enumerate(["Assumption", "Value", "Unit", "Notes"]):
    sc.cell(row=SC_HEADER, column=i + 1, value=label)
style_header(sc, SC_HEADER, 4)

SCALING_INPUTS = [
    ("manager_capacity", "Patients the manager alone keeps up with", 150, "patients",
     "Manager (t3.medium) also runs RabbitMQ + celery beat, so less than a dedicated worker. "
     "Below this, 0 workers needed. CALIBRATE with queue-lag metrics.", NUM),
    ("patients_per_worker", "Additional patients per worker box", 250, "patients/worker",
     "Each m5.large worker runs ~6 concurrent tasks (2*vCPU+2). 6-min cycle; tasks expire at "
     "5:30, so the batch must drain in time. Processing is NOT auto-scaled — workers are added "
     "by hand. CALIBRATE.", NUM),
    ("patients_per_web", "Patients per web instance", 2500, "patients/instance",
     "Upload endpoints are lightweight. ASG Max=2 today — beyond 2 instances, raise MaxSize. CALIBRATE.", NUM),
    ("web_min", "Minimum web instances", 1, "instances", "ASG MinSize.", NUM),
    ("chunks_per_patient_day", "ChunkRegistry rows per patient / day", 89, "rows/day",
     "From sample (7,107 chunks / 8 patients / ~10 days). Stream-dependent.", NUM),
    ("kb_per_db_row", "Bytes per DB row incl. indexes", 0.8, "KB/row",
     "ChunkRegistry + UploadTracking average. Rough.", NUM2),
    ("db_overhead", "DB headroom / overhead factor", 1.5, "x",
     "Indexes, bloat, free space, and other tables (SummaryStatisticDaily, etc.).", NUM2),
    ("w_xlarge", "Workers => bump RDS to db.m5.xlarge", 2, "workers",
     "DB connection + CPU pressure: every web instance AND every celery process holds "
     "connections. db.m5.large ~= 830 max connections. CALIBRATE.", NUM),
    ("w_2xlarge", "Workers => bump RDS to db.m5.2xlarge", 5, "workers", "", NUM),
    ("w_4xlarge", "Workers => bump RDS to db.m5.4xlarge", 11, "workers", "", NUM),
]
S = {}
r = SC_HEADER + 1
for key, label, value, unit, note, fmt in SCALING_INPUTS:
    sc.cell(row=r, column=1, value=label).border = BORDER
    c = sc.cell(row=r, column=2, value=value)
    c.fill = INPUT_FILL
    c.border = BORDER
    c.number_format = fmt
    sc.cell(row=r, column=3, value=unit).border = BORDER
    n = sc.cell(row=r, column=4, value=note)
    n.alignment = WRAP
    n.font = NOTE_FONT
    n.border = BORDER
    sc.row_dimensions[r].height = 34
    S[key] = r
    r += 1


def scl(key):
    return f"Scaling!$B${S[key]}"


def rds_rate_expr(wcell):
    """RDS hourly-rate expression (no leading '='), chosen by worker count."""
    return (f"IF({wcell}>={scl('w_4xlarge')},{price('rds_m5_4xlarge')},"
            f"IF({wcell}>={scl('w_2xlarge')},{price('rds_m5_2xlarge')},"
            f"IF({wcell}>={scl('w_xlarge')},{price('rds_m5_xlarge')},"
            f"{price('rds_m5_large')})))")


def rds_class_expr(wcell):
    return (f'IF({wcell}>={scl("w_4xlarge")},"db.m5.4xlarge",'
            f'IF({wcell}>={scl("w_2xlarge")},"db.m5.2xlarge",'
            f'IF({wcell}>={scl("w_xlarge")},"db.m5.xlarge","db.m5.large")))')


def workers_expr(pcell):
    return f"CEILING(MAX(0,{pcell}-{scl('manager_capacity')})/{scl('patients_per_worker')},1)"


def web_expr(pcell):
    return f"MAX({scl('web_min')},CEILING({pcell}/{scl('patients_per_web')},1))"


def rds_storage_expr(pcell):
    return (f"MAX(50,ROUNDUP({pcell}*{inv('months_collection')}*"
            f"({scl('chunks_per_patient_day')}+{inv('uploads_pt_day')})*{inv('days_month')}*"
            f"{scl('kb_per_db_row')}/1000000*{scl('db_overhead')},0))")


# --- Derived capacity for the single (Inputs) scenario ---
r += 1
sc.cell(row=r, column=1, value="Derived capacity for the current Inputs scenario").font = H2_FONT
r += 1
NP = inv("n_patients")
derived_caps = [
    ("d_workers", "Worker boxes required (m5.large)", f"=ROUND({workers_expr(NP)},0)", NUM),
    ("d_web", "Web instances required", f"=ROUND({web_expr(NP)},0)", NUM),
    ("d_rds_storage", "RDS storage required (GB)", f"=ROUND({rds_storage_expr(NP)},0)", NUM),
]
for key, label, formula, fmt in derived_caps:
    sc.cell(row=r, column=1, value=label).border = BORDER
    c = sc.cell(row=r, column=2, value=formula)
    c.fill = DERIVED_FILL
    c.border = BORDER
    c.number_format = fmt
    S[key] = r
    r += 1
# RDS class + rate reference d_workers
sc.cell(row=r, column=1, value="RDS instance class").border = BORDER
c = sc.cell(row=r, column=2, value=f"={rds_class_expr(scl('d_workers'))}")
c.fill = DERIVED_FILL
c.border = BORDER
S["d_rds_class"] = r
r += 1
sc.cell(row=r, column=1, value="RDS instance rate ($/hr)").border = BORDER
c = sc.cell(row=r, column=2, value=f"={rds_rate_expr(scl('d_workers'))}")
c.fill = DERIVED_FILL
c.border = BORDER
c.number_format = MONEY
S["d_rds_rate"] = r
r += 1

# --- Threshold documentation block ---
r += 1
sc.cell(row=r, column=1, value="Where the step-functions are").font = H2_FONT
r += 1
doc = [
    ("Processing workers", "Files per 6-min cycle (patients x upload cadence). ~6 concurrent "
     "tasks/m5.large; tasks expire at 5:30. NOT auto-scaled — added manually.",
     "Add m5.large workers"),
    ("RDS storage", "ChunkRegistry (~1 row/chunk) + UploadTracking (~1 row/upload) grow without "
     "bound; flagged 'very large on production' in CLAUDE.md.", "Raise allocation; gp2 gives only 3 IOPS/GB -> maybe gp3/io1"),
    ("RDS instance class", "DB connections + query CPU from web instances and every celery "
     "process. db.m5.large ~830 max connections.", "Step to xlarge / 2xlarge / 4xlarge"),
    ("Web tier", "Mobile upload request rate. ASG Max=2 today.", "Raise MaxSize / instance size"),
    ("RabbitMQ (on manager)", "Queue backlog held in manager RAM (4 GB) if processing lags.", "Bigger manager or dedicated broker"),
    ("S3 / DynamoDB / CLB", "Effectively unbounded. DDB is on-demand (auto-scales); per-study "
     "hot-partition is mitigated by the WRITE_STUDY_ROLLUP toggle. CLB auto-scales.", "Nothing (genuinely linear)"),
]
for i, label in enumerate(["Component", "What pushes it", "Action at threshold"]):
    sc.cell(row=r, column=i + 1, value=label)
style_header(sc, r, 3)
r += 1
for comp, push, action in doc:
    sc.cell(row=r, column=1, value=comp).border = BORDER
    a = sc.cell(row=r, column=2, value=push)
    a.alignment = WRAP
    a.font = NOTE_FONT
    a.border = BORDER
    b = sc.cell(row=r, column=3, value=action)
    b.alignment = WRAP
    b.font = NOTE_FONT
    b.border = BORDER
    sc.row_dimensions[r].height = 40
    r += 1
set_widths(sc, {"A": 38, "B": 52, "C": 40, "D": 40})

# ---------------------------------------------------------------------------
# SAVINGS PLAN sheet (picker: choose a commitment, see the discounted compute)
# ---------------------------------------------------------------------------
sp_ws = wb.create_sheet("Savings Plan")
sp_ws["A1"] = "Savings Plan / Reserved Instance picker"
sp_ws["A1"].font = TITLE_FONT
sp_ws["A2"] = ("Pick a commitment in the yellow cell below. Savings Plans (EC2) and Reserved "
               "Instances (RDS) discount COMPUTE only — the EC2 servers and the RDS database "
               "instance. Storage (S3, EBS, RDS storage), the load balancer, DynamoDB, and data "
               "transfer always bill on-demand. Discount %s are approximate us-east-1 list values "
               "— overwrite them with a real quote from the AWS calculator or your account rep.")
sp_ws["A2"].font = NOTE_FONT
sp_ws["A2"].alignment = WRAP
sp_ws.merge_cells("A2:E2")
sp_ws.row_dimensions[2].height = 48

sp_ws["A4"] = "Selected plan:"
sp_ws["A4"].font = BOLD
pick = sp_ws["B4"]
pick.value = "None (on-demand)"
pick.fill = INPUT_FILL
pick.font = BOLD
pick.border = BORDER
sp_ws.merge_cells("B4:C4")
sp_ws["A5"] = "EC2 / compute discount applied"
ec2cell = sp_ws["B5"]
ec2cell.value = "=VLOOKUP($B$4,$A$9:$C$13,2,FALSE)"
ec2cell.number_format = PCT
ec2cell.fill = DERIVED_FILL
ec2cell.border = BORDER
sp_ws["A6"] = "RDS discount applied"
rdscell = sp_ws["B6"]
rdscell.value = "=VLOOKUP($B$4,$A$9:$C$13,3,FALSE)"
rdscell.number_format = PCT
rdscell.fill = DERIVED_FILL
rdscell.border = BORDER

SP_HDR = 8
for i, label in enumerate(["Plan", "EC2 / Compute SP discount", "RDS RI discount", "Term / payment"]):
    sp_ws.cell(row=SP_HDR, column=i + 1, value=label)
style_header(sp_ws, SP_HDR, 4)
plans = [
    ("None (on-demand)", 0.0, 0.0, "No commitment"),
    ("1-year, no upfront", 0.27, 0.30, "1 yr, $0 upfront"),
    ("1-year, all upfront", 0.30, 0.34, "1 yr, paid upfront"),
    ("3-year, no upfront", 0.45, 0.48, "3 yr, $0 upfront"),
    ("3-year, all upfront", 0.50, 0.53, "3 yr, paid upfront"),
]
r = SP_HDR + 1
for name, e, rd, term in plans:
    sp_ws.cell(row=r, column=1, value=name).border = BORDER
    c = sp_ws.cell(row=r, column=2, value=e); c.number_format = PCT; c.fill = INPUT_FILL; c.border = BORDER
    c = sp_ws.cell(row=r, column=3, value=rd); c.number_format = PCT; c.fill = INPUT_FILL; c.border = BORDER
    sp_ws.cell(row=r, column=4, value=term).border = BORDER
    r += 1

dv = DataValidation(type="list", formula1="$A$9:$A$13", allow_blank=False)
sp_ws.add_data_validation(dv)
dv.add(pick)

r += 1
sp_ws.cell(row=r, column=1, value="Compute savings at the current Inputs scenario").font = H2_FONT
r += 1
_od_ec2 = (f"(({scl('d_web')}+1)*{price('ec2_t3_medium')}*{inv('hours_month')}"
           f"+{scl('d_workers')}*{price('ec2_m5_large')}*{inv('hours_month')})")
_od_rds = f"({scl('d_rds_rate')}*{inv('hours_month')})"
summ = [
    ("On-demand compute (EC2 + RDS) / mo", f"={_od_ec2}+{_od_rds}", MONEY, None),
    ("With selected plan / mo", f"={_od_ec2}*(1-$B$5)+{_od_rds}*(1-$B$6)", MONEY, None),
    ("Monthly savings", None, MONEY, SUBTOTAL_FILL),
    ("Annual savings", None, MONEY0, SUBTOTAL_FILL),
    ("Effective discount on compute", None, PCT, None),
]
srow = {}
for label, formula, fmt, fill in summ:
    lc = sp_ws.cell(row=r, column=1, value=label); lc.font = BOLD; lc.border = BORDER
    vc = sp_ws.cell(row=r, column=2)
    if formula:
        vc.value = formula
    vc.number_format = fmt
    vc.border = BORDER
    if fill:
        lc.fill = fill; vc.fill = fill
    srow[label] = r
    r += 1
sp_ws.cell(row=srow["Monthly savings"], column=2,
           value=f"=B{srow['On-demand compute (EC2 + RDS) / mo']}-B{srow['With selected plan / mo']}")
sp_ws.cell(row=srow["Annual savings"], column=2, value=f"=B{srow['Monthly savings']}*12")
sp_ws.cell(row=srow["Effective discount on compute"], column=2,
           value=f"=IFERROR(B{srow['Monthly savings']}/B{srow['On-demand compute (EC2 + RDS) / mo']},0)")
sp_ws.cell(row=r + 1, column=1,
           value="Note: Savings Plans and RDS Reserved Instances are separate commitments; "
                 "shown together here for convenience. Lambda is also SP-eligible but immaterial.").font = NOTE_FONT
set_widths(sp_ws, {"A": 40, "B": 16, "C": 16, "D": 22})
sp_ws.sheet_properties.tabColor = "FFC000"

# Discount multipliers applied to compute lines on Fixed Costs and Tiers.
DISC_EC2 = "(1-'Savings Plan'!$B$5)"
DISC_RDS = "(1-'Savings Plan'!$B$6)"

# ---------------------------------------------------------------------------
# FIXED COSTS sheet
# ---------------------------------------------------------------------------
fx = wb.create_sheet("Fixed Costs")
fx["A1"] = "Fixed / fixed-ish monthly costs (step up with patient count via the Scaling sheet)"
fx["A1"].font = TITLE_FONT
FX_HEADER = 3
for i, label in enumerate(["Component", "Detail", "Qty", "Unit cost basis", "Monthly cost", "Notes"]):
    fx.cell(row=FX_HEADER, column=i + 1, value=label)
style_header(fx, FX_HEADER, 6)

H = inv("hours_month")
fx_rows = [
    ("EB web tier", "t3.medium x web instances (derived)", f"={scl('d_web')}",
     "instances x t3.medium/hr x hrs",
     f"={scl('d_web')}*{price('ec2_t3_medium')}*{H}*{DISC_EC2}",
     "Derived from patients (Scaling). Autoscales; raise MaxSize beyond 2. Savings Plan applies."),
    ("Classic Load Balancer", "1 CLB", 1, "CLB/hr x hrs",
     f"=1*{price('clb_hour')}*{H}",
     "Always running. + $0.008/GB processed (not modelled; small). NOT Savings-Plan eligible."),
    ("Processing manager", "t3.medium x 1", 1, "1 x t3.medium/hr x hrs",
     f"=1*{price('ec2_t3_medium')}*{H}*{DISC_EC2}",
     "Celery + RabbitMQ broker run here (no separate broker cost). Savings Plan applies."),
    ("Processing workers", "m5.large x workers (derived)", f"={scl('d_workers')}",
     "workers x m5.large/hr x hrs",
     f"={scl('d_workers')}*{price('ec2_m5_large')}*{H}*{DISC_EC2}",
     "Derived from patients (Scaling). NOT auto-scaled in Beiwe — manual provisioning. Savings Plan applies."),
    ("RDS instance", "class derived from workers", f"={scl('d_rds_class')}",
     "chosen class/hr x hrs",
     f"={scl('d_rds_rate')}*{H}*{DISC_RDS}",
     "Steps large->xlarge->2xlarge->4xlarge as load grows. RDS Reserved Instance discount applies."),
    ("RDS storage", "gp2 (derived from DB growth)", f"={scl('d_rds_storage')}",
     "GB x $/GB-mo",
     f"={scl('d_rds_storage')}*{price('rds_gp2')}",
     "Grows with ChunkRegistry + UploadTracking. Min 50 GB."),
    ("RDS backups", "35-day retention", 0, "GB beyond free x $/GB-mo",
     f"=0*{price('rds_backup')}",
     "Free up to 100% of provisioned storage. Set qty>0 if backup volume exceeds that."),
    ("EBS volumes", "root volumes (web + mgr + workers)",
     f"=({scl('d_web')}+1+{scl('d_workers')})*{inv('ebs_gb_per_instance')}",
     "total GB x $/GB-mo",
     f"=({scl('d_web')}+1+{scl('d_workers')})*{inv('ebs_gb_per_instance')}*{price('ebs_gp3')}",
     "Root disks for EC2 instances (RDS storage counted separately)."),
    ("Secrets Manager", "deploy credentials", 1, "secrets x $/secret-mo",
     f"=1*{price('secrets')}", "beiwe-deploy-credentials (prerequisites stack)."),
    ("Route 53", "1 hosted zone", 1, "zone x $/mo", f"=1*{price('route53')}", "Domain hosting (if used)."),
    ("CloudWatch", "logs + metrics", 1, "estimate", f"=1*{price('cloudwatch')}",
     "Rough allowance incl. metadata-index Lambda logs/metrics."),
    ("Metadata index (idle baseline)", "DynamoDB + SQS + Lambda + EventBridge", 1, "pay-per-request",
     "=0", "Cost $0 when idle; usage-based cost is in Per-Patient Costs."),
    ("Nightly pause scheduler", "EventBridge Scheduler + Lambda", 1, "optional",
     "=0", "feat/ut-setup option; negligible to run. Would REDUCE compute lines if enabled. Not applied in this always-on model."),
]
r = FX_HEADER + 1
fx_first = r
for comp, detail, qty, basis, monthly, note in fx_rows:
    fx.cell(row=r, column=1, value=comp).border = BORDER
    fx.cell(row=r, column=2, value=detail).border = BORDER
    qc = fx.cell(row=r, column=3, value=qty)
    qc.border = BORDER
    qc.number_format = NUM2
    fx.cell(row=r, column=4, value=basis).border = BORDER
    mc = fx.cell(row=r, column=5, value=monthly)
    mc.number_format = MONEY
    mc.border = BORDER
    nc = fx.cell(row=r, column=6, value=note)
    nc.font = NOTE_FONT
    nc.alignment = WRAP
    nc.border = BORDER
    fx.row_dimensions[r].height = 28
    r += 1
fx_last = r - 1
fx.cell(row=r, column=1, value="FIXED MONTHLY SUBTOTAL").font = BOLD
sub = fx.cell(row=r, column=5, value=f"=SUM(E{fx_first}:E{fx_last})")
sub.number_format = MONEY
sub.font = BOLD
for c in range(1, 7):
    fx.cell(row=r, column=c).fill = SUBTOTAL_FILL
FX_SUBTOTAL = r
set_widths(fx, {"A": 26, "B": 32, "C": 12, "D": 22, "E": 14, "F": 48})

# ---------------------------------------------------------------------------
# PER-PATIENT COSTS sheet
# ---------------------------------------------------------------------------
pp = wb.create_sheet("Per-Patient Costs")
pp["A1"] = "Per-patient (variable) monthly costs"
pp["A1"].font = TITLE_FONT
pp["A2"] = ("These scale linearly with patients (no step-functions). Storage is cumulative: "
            "the figures are the recurring monthly bill for the FULL dataset once all patients "
            "have completed collection (the steady-state peak).")
pp["A2"].font = NOTE_FONT
pp["A2"].alignment = WRAP
pp.merge_cells("A2:D2")
pp.row_dimensions[2].height = 30

pp["A4"] = "Derived dataset totals"
pp["A4"].font = H2_FONT
D = {}
derived = [
    ("total_chunked_gb", "Total chunked data stored (GB)",
     f"={inv('n_patients')}*{inv('months_collection')}*{inv('chunked_gb_pt_mo')}", NUM2),
    ("total_raw_gb", "Total raw uploads stored (GB)", None, NUM2),
    ("total_gb", "Total S3 data stored (GB)", None, NUM2),
    ("glacier_gb", "  ... in Glacier Deep Archive (GB)", None, NUM2),
    ("standard_gb", "  ... in S3 Standard (GB)", None, NUM2),
    ("uploads_month", "Raw uploads per month (all patients)",
     f"={inv('n_patients')}*{inv('uploads_pt_day')}*{inv('days_month')}", NUM),
]
r = 5
for key, label, formula, fmt in derived:
    pp.cell(row=r, column=1, value=label).border = BORDER
    cell = pp.cell(row=r, column=2)
    cell.fill = DERIVED_FILL
    cell.border = BORDER
    cell.number_format = fmt
    D[key] = r
    if formula:
        cell.value = formula
    r += 1
pp.cell(row=D["total_raw_gb"], column=2, value=f"=B{D['total_chunked_gb']}*{inv('raw_multiplier')}")
pp.cell(row=D["total_gb"], column=2, value=f"=B{D['total_chunked_gb']}+B{D['total_raw_gb']}")
pp.cell(row=D["glacier_gb"], column=2, value=f"=B{D['total_gb']}*{inv('glacier_fraction')}")
pp.cell(row=D["standard_gb"], column=2, value=f"=B{D['total_gb']}-B{D['glacier_gb']}")

PP_HEADER = r + 1
for i, label in enumerate(["Component", "Basis", "Monthly cost", "Notes"]):
    pp.cell(row=PP_HEADER, column=i + 1, value=label)
style_header(pp, PP_HEADER, 4)

UP = f"B{D['uploads_month']}"
pp_rows = [
    ("S3 storage — Standard", "standard GB x $/GB-mo", f"=B{D['standard_gb']}*{price('s3_standard')}",
     "Raw + chunked, compressed. The dominant per-patient cost."),
    ("S3 storage — Glacier Deep Archive", "archived GB x $/GB-mo", f"=B{D['glacier_gb']}*{price('s3_glacier_da')}",
     "Only if a fraction is archived (Inputs)."),
    ("S3 PUT requests", "uploads x 2 (raw + chunk write)", f"={UP}*2/1000*{price('s3_put')}",
     "Raw upload write + processed chunk write per upload."),
    ("S3 GET requests", "uploads x 1 (processing reads)", f"={UP}*1/1000*{price('s3_get')}",
     "Processing pipeline reads. Tiny."),
    ("DynamoDB metadata index — writes", "uploads x writes/upload x $/M WRU",
     f"={UP}*{inv('ddb_writes_per_upload')}/1000000*{price('ddb_wru')}", "5 writes/upload (Inputs). On-demand."),
    ("DynamoDB metadata index — storage", "90-day marker retention",
     f"={inv('n_patients')}*{inv('uploads_pt_day')}*90*0.0000003*{price('ddb_storage')}",
     "~0.3 KB/marker, TTL 90 days, + small rollups. Negligible."),
    ("SQS (metadata index)", "max(0, msgs - 1M free) x $/M", f"=MAX(0,{UP}-1000000)/1000000*{price('sqs_req')}",
     "1 message/upload; first 1M/mo free."),
    ("Lambda (metadata index)", "invocations (batches of 10) + GB-sec",
     f"=({UP}/10/1000000*{price('lambda_req')})+({UP}/10*0.256*0.2*{price('lambda_gbsec')})",
     "256 MB, ~0.2 s/batch of 10. Mostly free-tier."),
    ("Data egress", "max(0, GB - 100 free) x $/GB", f"=MAX(0,{inv('egress_gb_mo')}-100)*{price('dto')}",
     "Researcher downloads of participant data."),
]
r = PP_HEADER + 1
pp_first = r
for comp, basis, monthly, note in pp_rows:
    pp.cell(row=r, column=1, value=comp).border = BORDER
    pp.cell(row=r, column=2, value=basis).border = BORDER
    mc = pp.cell(row=r, column=3, value=monthly)
    mc.number_format = MONEY
    mc.border = BORDER
    nc = pp.cell(row=r, column=4, value=note)
    nc.font = NOTE_FONT
    nc.alignment = WRAP
    nc.border = BORDER
    pp.row_dimensions[r].height = 28
    r += 1
pp_last = r - 1
pp.cell(row=r, column=1, value="PER-PATIENT MONTHLY SUBTOTAL (full dataset)").font = BOLD
sub = pp.cell(row=r, column=3, value=f"=SUM(C{pp_first}:C{pp_last})")
sub.number_format = MONEY
sub.font = BOLD
for c in range(1, 5):
    pp.cell(row=r, column=c).fill = SUBTOTAL_FILL
PP_SUBTOTAL = r
r += 1
pp.cell(row=r, column=1, value="Per patient, per month of participation").font = BOLD
perpt = pp.cell(row=r, column=3, value=f"=C{PP_SUBTOTAL}/{inv('n_patients')}/{inv('months_collection')}")
perpt.number_format = MONEY
perpt.font = BOLD
PP_PERPT = r
set_widths(pp, {"A": 34, "B": 34, "C": 14, "D": 50})

# ---------------------------------------------------------------------------
# TOTALS sheet
# ---------------------------------------------------------------------------
tt = wb.create_sheet("Totals")
tt["A1"] = "Total cost summary (current Inputs scenario)"
tt["A1"].font = TITLE_FONT
tt["A2"] = ("Always-on production in us-east-1, including the metadata-index layer. Server "
            "capacity is derived from the patient count; see the Tiers sheet for the step-ups.")
tt["A2"].font = NOTE_FONT
tt["A2"].alignment = WRAP
tt.merge_cells("A2:C2")

rows = [
    ("Fixed monthly costs (servers, LB, RDS, overhead)", f"='Fixed Costs'!E{FX_SUBTOTAL}", MONEY, SUBTOTAL_FILL),
    ("Per-patient monthly costs (storage, requests, index)", f"='Per-Patient Costs'!C{PP_SUBTOTAL}", MONEY, SUBTOTAL_FILL),
    ("TOTAL MONTHLY", None, MONEY, TOTAL_FILL),
    ("TOTAL ANNUAL", None, MONEY, TOTAL_FILL),
    ("", None, None, None),
    ("Number of patients", f"={inv('n_patients')}", NUM, None),
    ("Total cost per patient / month (all-in)", None, MONEY, None),
    ("Variable cost per patient / month of participation", f"='Per-Patient Costs'!C{PP_PERPT}", MONEY, None),
    ("", None, None, None),
    ("STUDY TOTAL — collection period", None, None, None),
    ("Months of data collection", f"={inv('months_collection')}", NUM, None),
    ("Approx. total to run the platform for the study", None, MONEY0, TOTAL_FILL),
    ("Approx. total per patient (whole study)", None, MONEY, None),
    ("Storage-only cost/mo after collection (retention)", f"='Per-Patient Costs'!C{PP_SUBTOTAL}", MONEY, None),
]
r = 4
ref = {}
for label, formula, fmt, fill in rows:
    if label == "":
        r += 1
        continue
    lc = tt.cell(row=r, column=1, value=label)
    lc.font = BOLD if label.isupper() or "TOTAL" in label or "STUDY" in label else BOLD
    vc = tt.cell(row=r, column=2)
    if formula:
        vc.value = formula
    if fmt:
        vc.number_format = fmt
    if fill:
        lc.fill = fill
        vc.fill = fill
    lc.border = BORDER
    vc.border = BORDER
    ref[label] = r
    r += 1
tt.cell(row=ref["STUDY TOTAL — collection period"], column=1).fill = SECTION_FILL
tt.cell(row=ref["STUDY TOTAL — collection period"], column=2).fill = SECTION_FILL
tt.cell(row=ref["TOTAL MONTHLY"], column=2,
        value=f"=B{ref['Fixed monthly costs (servers, LB, RDS, overhead)']}"
              f"+B{ref['Per-patient monthly costs (storage, requests, index)']}")
tt.cell(row=ref["TOTAL ANNUAL"], column=2, value=f"=B{ref['TOTAL MONTHLY']}*12")
tt.cell(row=ref["Total cost per patient / month (all-in)"], column=2,
        value=f"=B{ref['TOTAL MONTHLY']}/{inv('n_patients')}")
tt.cell(row=ref["Approx. total to run the platform for the study"], column=2,
        value=f"=B{ref['TOTAL MONTHLY']}*{inv('months_collection')}")
tt.cell(row=ref["Approx. total per patient (whole study)"], column=2,
        value=f"=B{ref['Approx. total to run the platform for the study']}/{inv('n_patients')}")
# notes under the table
note_r = r + 1
for line in [
    "Includes: AWS infrastructure for data collection, storage, and query only.",
    "Excludes: personnel, participant devices/incentives, and analysis compute.",
    "Fixed infrastructure runs continuously and dominates at this scale; after collection ends "
    "you can pause/downsize servers and retain data cheaply (S3 / Glacier).",
    "Pricing is us-east-1 on-demand list price as of the Pricing-sheet build date — a dynamic "
    "estimate, not a fixed quote.",
]:
    c = tt.cell(row=note_r, column=1, value=line)
    c.font = NOTE_FONT
    c.alignment = WRAP
    tt.merge_cells(start_row=note_r, start_column=1, end_row=note_r, end_column=4)
    tt.row_dimensions[note_r].height = 28
    note_r += 1
set_widths(tt, {"A": 52, "B": 18})

# ---------------------------------------------------------------------------
# TIERS sheet  (recompute the whole stack across patient counts)
# ---------------------------------------------------------------------------
ti = wb.create_sheet("Tiers")
ti["A1"] = "Cost by patient tier — where the step-ups happen"
ti["A1"].font = TITLE_FONT
ti["A2"] = ("Each column recomputes the full stack from its patient count, using the same "
            "Scaling ratios, Pricing, and per-patient assumptions. Edit the patient counts in "
            "row 4. Watch Workers and the RDS class/storage step up across the columns. "
            "The 25-patient column is the planned grant scenario.")
ti["A2"].font = NOTE_FONT
ti["A2"].alignment = WRAP
ti.merge_cells("A2:G2")
ti.row_dimensions[2].height = 30

TIERS = [25, 100, 500, 1000, 5000, 25000]
TCOLS = ["B", "C", "D", "E", "F", "G"]
PROW = 4
ti.cell(row=PROW, column=1, value="Enrolled patients").font = BOLD
for col, val in zip(TCOLS, TIERS):
    cell = ti[f"{col}{PROW}"]
    cell.value = val
    cell.fill = INPUT_FILL
    cell.font = BOLD
    cell.number_format = NUM
    cell.border = BORDER
    cell.alignment = Alignment(horizontal="right")

H = inv("hours_month")


def pc(col):  # patient cell for a column
    return f"{col}${PROW}"


def write_tier_section(ws, row, title):
    ws.cell(row=row, column=1, value=title).font = H2_FONT
    for c in range(1, 7):
        ws.cell(row=row, column=c).fill = SECTION_FILL
    return row + 1


def write_tier_row(ws, row, label, formula_fn, fmt, bold=False, fill=None):
    lc = ws.cell(row=row, column=1, value=label)
    if bold:
        lc.font = BOLD
    lc.border = BORDER
    if fill:
        lc.fill = fill
    for col in TCOLS:
        cell = ws.cell(row=row, column="ABCDEFGH".index(col) + 1, value=formula_fn(col))
        cell.number_format = fmt
        cell.border = BORDER
        if bold:
            cell.font = BOLD
        if fill:
            cell.fill = fill
    return row + 1


r = PROW + 1
# --- derived capacity ---
r = write_tier_section(ti, r, "Derived capacity")
workers_row = r
r = write_tier_row(ti, r, "Worker boxes (m5.large)", lambda c: f"=ROUND({workers_expr(pc(c))},0)", NUM)
web_row = r
r = write_tier_row(ti, r, "Web instances", lambda c: f"=ROUND({web_expr(pc(c))},0)", NUM)
rdsstore_row = r
r = write_tier_row(ti, r, "RDS storage (GB)", lambda c: f"=ROUND({rds_storage_expr(pc(c))},0)", NUM)
rdsclass_row = r
r = write_tier_row(ti, r, "RDS instance class",
                   lambda c: f"={rds_class_expr(f'{c}${workers_row}')}", "General")

# --- fixed monthly ---
r = write_tier_section(ti, r, "Fixed monthly cost")
fx_rows_t = []
r = write_tier_row(ti, r, "EB web tier", lambda c: f"={c}${web_row}*{price('ec2_t3_medium')}*{H}*{DISC_EC2}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "Classic Load Balancer", lambda c: f"={price('clb_hour')}*{H}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "Processing manager", lambda c: f"={price('ec2_t3_medium')}*{H}*{DISC_EC2}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "Processing workers", lambda c: f"={c}${workers_row}*{price('ec2_m5_large')}*{H}*{DISC_EC2}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "RDS instance", lambda c: f"=({rds_rate_expr(f'{c}${workers_row}')})*{H}*{DISC_RDS}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "RDS storage", lambda c: f"={c}${rdsstore_row}*{price('rds_gp2')}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "EBS volumes", lambda c: f"=({c}${web_row}+1+{c}${workers_row})*{inv('ebs_gb_per_instance')}*{price('ebs_gp3')}", MONEY); fx_rows_t.append(r-1)
r = write_tier_row(ti, r, "Overhead (Secrets, R53, CloudWatch)", lambda c: f"={price('secrets')}+{price('route53')}+{price('cloudwatch')}", MONEY); fx_rows_t.append(r-1)
fx_sub_t = r
r = write_tier_row(ti, r, "Fixed subtotal", lambda c: f"=SUM({c}{fx_rows_t[0]}:{c}{fx_rows_t[-1]})", MONEY, bold=True, fill=SUBTOTAL_FILL)

# --- per-patient monthly ---
r = write_tier_section(ti, r, "Per-patient monthly cost (full dataset)")


def total_gb(c):
    return f"({pc(c)}*{inv('months_collection')}*{inv('chunked_gb_pt_mo')}*(1+{inv('raw_multiplier')}))"


def up_mo(c):
    return f"({pc(c)}*{inv('uploads_pt_day')}*{inv('days_month')})"


pp_rows_t = []
r = write_tier_row(ti, r, "S3 storage (std + glacier)",
                   lambda c: f"={total_gb(c)}*(1-{inv('glacier_fraction')})*{price('s3_standard')}+"
                             f"{total_gb(c)}*{inv('glacier_fraction')}*{price('s3_glacier_da')}", MONEY); pp_rows_t.append(r-1)
r = write_tier_row(ti, r, "S3 requests",
                   lambda c: f"={up_mo(c)}*2/1000*{price('s3_put')}+{up_mo(c)}/1000*{price('s3_get')}", MONEY); pp_rows_t.append(r-1)
r = write_tier_row(ti, r, "Metadata index (DDB+SQS+Lambda)",
                   lambda c: f"={up_mo(c)}*{inv('ddb_writes_per_upload')}/1000000*{price('ddb_wru')}+"
                             f"{pc(c)}*{inv('uploads_pt_day')}*90*0.0000003*{price('ddb_storage')}+"
                             f"MAX(0,{up_mo(c)}-1000000)/1000000*{price('sqs_req')}+"
                             f"({up_mo(c)}/10/1000000*{price('lambda_req')})+({up_mo(c)}/10*0.256*0.2*{price('lambda_gbsec')})", MONEY); pp_rows_t.append(r-1)
pp_sub_t = r
r = write_tier_row(ti, r, "Per-patient subtotal", lambda c: f"=SUM({c}{pp_rows_t[0]}:{c}{pp_rows_t[-1]})", MONEY, bold=True, fill=SUBTOTAL_FILL)

# --- totals ---
r = write_tier_section(ti, r, "Totals")
tot_row = r
r = write_tier_row(ti, r, "TOTAL MONTHLY", lambda c: f"={c}{fx_sub_t}+{c}{pp_sub_t}", MONEY, bold=True, fill=TOTAL_FILL)
r = write_tier_row(ti, r, "TOTAL ANNUAL", lambda c: f"={c}{tot_row}*12", MONEY0, bold=True, fill=TOTAL_FILL)
r = write_tier_row(ti, r, "All-in $ / patient / month", lambda c: f"={c}{tot_row}/{pc(c)}", MONEY, bold=True)
ti.cell(row=r, column=1, value="Note: egress assumed 0 here (see Per-Patient Costs sheet for egress).").font = NOTE_FONT
set_widths(ti, {"A": 36, "B": 13, "C": 13, "D": 13, "E": 13, "F": 13, "G": 13})

# ---------------------------------------------------------------------------
# SAMPLE DATA sheet
# ---------------------------------------------------------------------------
sd = wb.create_sheet("Sample Data")
sd["A1"] = "Storage anchor — measured sample datasets"
sd["A1"].font = TITLE_FONT
notes = [
    "Source: s3://beiwe-data-kowalski-beiwe-rxjfjw9miockp1gzxa42gd2zikzdrd3ndrclz",
    "Prefix: CHUNKED_DATA/2grtzwKjSxi64uYkxqZASgxe/  (public Zenodo-style sample data)",
    "Measured 2026-06-24 via `aws s3 ls --recursive` (sizes are the compressed .zst bytes — the S3 billing basis).",
    "8 patients, 7,107 objects, 1,117,493,665 bytes (1.041 GiB) total.",
    "Avg 133 MiB/patient over ~8-22 day active spans => ~13 MiB/patient/day.",
    "~89 ChunkRegistry rows/patient/day (this anchors the DB-growth assumption on the Scaling sheet).",
    "This is CHUNKED (processed) data only. Raw uploads are added via the 'raw multiplier' input.",
    "Storage is dominated by motion sensors — a study without them would be far smaller.",
]
r = 3
for n in notes:
    sd.cell(row=r, column=1, value=n).font = NOTE_FONT
    r += 1
r += 1
sd.cell(row=r, column=1, value="Per data stream (sample aggregate)").font = H2_FONT
r += 1
for i, label in enumerate(["Data stream", "Size (MiB)", "% of bytes"]):
    sd.cell(row=r, column=i + 1, value=label)
style_header(sd, r, 3)
r += 1
streams = [
    ("accelerometer", 582.4, 0.546), ("gyro", 274.2, 0.257), ("magnetometer", 155.3, 0.146),
    ("devicemotion", 28.9, 0.027), ("gps", 9.2, 0.009), ("ios_log", 8.0, 0.008),
    ("bluetooth", 4.1, 0.004), ("wifi", 2.0, 0.002), ("app_log", 1.3, 0.001),
    ("power_state", 0.3, 0.0), ("reachability/texts/calls/identifiers/surveys", 0.1, 0.0),
]
for name, mib, frac in streams:
    sd.cell(row=r, column=1, value=name).border = BORDER
    c = sd.cell(row=r, column=2, value=mib); c.number_format = NUM2; c.border = BORDER
    c = sd.cell(row=r, column=3, value=frac); c.number_format = PCT; c.border = BORDER
    r += 1
set_widths(sd, {"A": 46, "B": 14, "C": 12})

# ---------------------------------------------------------------------------
# OVERVIEW sheet (first)
# ---------------------------------------------------------------------------
ov = wb.create_sheet("Overview", 0)
ov["A1"] = "Beiwe AWS Cost Worksheet"
ov["A1"].font = TITLE_FONT
blocks = [
    ("Purpose", [
        "A study-design / grant-budgeting estimate of the AWS cost to run Beiwe. Default scenario "
        "is the planned grant: ~20-25 patients with up to 90 days of monitoring each.",
        "Covers AWS infrastructure for data collection, storage, and query only — NOT personnel, "
        "devices, or analysis compute.",
    ]),
    ("Scenario", [
        "24/7 always-on production Beiwe deployment in AWS us-east-1.",
        "Includes the additive infrastructure from the feat/ut-setup branch:",
        "  - DynamoDB upload-metadata index (S3 -> EventBridge -> SQS -> Lambda -> DynamoDB).",
        "  - Dedicated VPC with NO NAT gateway (saves ~$35/mo vs. a private-subnet design).",
        "The nightly-pause scheduler is NOT applied here (it would reduce the compute lines); "
        "it is listed on Fixed Costs as an optional saving.",
    ]),
    ("Threshold-aware", [
        "Beiwe's compute and database STEP UP as enrollment grows — they are not flat.",
        "Worker count, RDS storage, and RDS instance class are derived from the patient count "
        "via tunable ratios on the Scaling sheet.",
        "The Tiers sheet recomputes the whole stack at 100 / 500 / 1k / 5k / 25k patients so the "
        "step-ups are visible. The processing tier is NOT auto-scaled — workers are added by hand.",
    ]),
    ("How to use", [
        "1. Edit the yellow cells on the Inputs sheet (patient count, study length, streams, etc.).",
        "2. Tune the step-function ratios on the Scaling sheet (flagged ones need production calibration).",
        "3. Unit rates live on the Pricing sheet (also editable) — update if AWS prices change.",
        "4. Read the Totals sheet for the current scenario, or the Tiers sheet for the scaling curve.",
        "Open in Excel / Google Sheets / LibreOffice so the formulas recalculate.",
    ]),
    ("Cost structure", [
        "FIXED / fixed-ish (Fixed Costs sheet): EB web tier, Classic LB, processing manager "
        "(also runs RabbitMQ), workers, RDS instance + storage + backups, EBS, and small overhead. "
        "These step up with patient count.",
        "PER-PATIENT (Per-Patient Costs sheet): S3 storage (raw + chunked), S3 requests, the "
        "DynamoDB metadata index, and egress. Genuinely linear; storage dominates and grows with study length.",
    ]),
    ("Cost-reduction levers (not applied in the base numbers)", [
        "Compute Savings Plans / Reserved Instances: ~40-50% off EC2 + RDS for a 1- or 3-year "
        "commitment — meaningful because the always-on servers dominate cost at small scale. "
        "Use the Savings Plan sheet to pick a term and see the discounted total.",
        "Nightly pause scheduler (feat/ut-setup): stops web + processing + RDS overnight for "
        "non-production environments.",
        "Glacier Deep Archive: ~25x cheaper than S3 Standard for long-term retention after "
        "collection ends (set the archive fraction on Inputs).",
        "After a study finishes collecting, pause/downsize the servers — retention is storage-only.",
    ]),
    ("Key assumptions to sanity-check", [
        "Storage per patient is anchored to a real sample (~13 MiB/day) BUT is dominated by motion "
        "sensors — confirm which streams the study actually collects.",
        "Raw-upload retention multiplier defaults to 1.0x chunked (total ~2x chunked). Set to 0 if "
        "raw files are deleted after processing.",
        "Scaling ratios (patients per worker, DB growth, RDS class breakpoints) are CONSERVATIVE "
        "rules of thumb — calibrate against production telemetry before quoting at large scale.",
        "Pricing is us-east-1 on-demand list price as of the build date; verify before quoting.",
    ]),
]
r = 3
for title, lines in blocks:
    ov.cell(row=r, column=1, value=title).font = H2_FONT
    r += 1
    for line in lines:
        c = ov.cell(row=r, column=1, value=line)
        c.alignment = WRAP
        ov.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        ov.row_dimensions[r].height = 15 + 13 * (len(line) // 95)
        r += 1
    r += 1
set_widths(ov, {"A": 18, "B": 14, "C": 14, "D": 14, "E": 14, "F": 14})

# tab colors
ov.sheet_properties.tabColor = "1F3864"
ti.sheet_properties.tabColor = "C00000"
tt.sheet_properties.tabColor = "C00000"
inp.sheet_properties.tabColor = "FFC000"
sc.sheet_properties.tabColor = "FFC000"

OUT = "beiwe_cost_worksheet.xlsx"
wb.save(OUT)
print(f"wrote {OUT}")
