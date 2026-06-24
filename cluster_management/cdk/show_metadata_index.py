#!/usr/bin/env python3
"""show_metadata_index.py -- pretty-print the Upload Metadata Index.

A read-only demo/ops view of the DynamoDB index that the metadata-index Lambda
populates from S3 object-created events. Renders: a live recent-uploads feed,
per-participant per-stream freshness (with stale-stream flags), study-level
upload volume over time as a bar chart, and per-stream totals.

It never touches S3, the upload path, or the Beiwe database -- it only reads the
derived index. (S3 stays the system of record.)

Usage:
    python show_metadata_index.py                          # profile shiny-dev, resolve table from the stack
    python show_metadata_index.py --profile shiny-dev --stale-hours 12
    python show_metadata_index.py --table <name> --region us-east-1 --no-color

NOTE: this SCANS the whole table to build a complete picture, which is fine for a
dev-sized index. A production dashboard should Query per study (begins_with on the
SK) rather than Scan -- see cluster_management/cdk/METADATA_INDEX.md.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone

import boto3

# Friendly display names for the common canonical streams; anything not listed
# falls back to a title-cased version of the raw name.
STREAM_DISPLAY = {
    "accelerometer": "Accelerometer", "gps": "GPS", "gyro": "Gyro",
    "magnetometer": "Magnetometer", "devicemotion": "Device Motion",
    "bluetooth": "Bluetooth", "wifi": "WiFi", "power_state": "Power State",
    "proximity": "Proximity", "reachability": "Reachability",
    "app_log": "Android Log", "ios_log": "iOS Log", "calls": "Calls",
    "texts": "Texts", "identifiers": "Identifiers",
    "survey_answers": "Survey Answers", "survey_timings": "Survey Timings",
    "audio_recordings": "Audio", "ambient_audio": "Ambient Audio",
}


class C:
    """ANSI color codes; blanked out when --no-color or non-tty."""
    BOLD = "\033[1m"; DIM = "\033[2m"; RESET = "\033[0m"
    GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"
    CYAN = "\033[36m"; BLUE = "\033[34m"; MAGENTA = "\033[35m"

    @classmethod
    def disable(cls):
        for name in ("BOLD", "DIM", "RESET", "GREEN", "YELLOW", "RED", "CYAN", "BLUE", "MAGENTA"):
            setattr(cls, name, "")


def num(v) -> int:
    """DynamoDB resource returns Decimal; coerce to int."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def human_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_time(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(iso: str, now: datetime) -> str:
    dt = parse_time(iso)
    if dt is None:
        return "?"
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def bar(value: int, peak: int, width: int = 32) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(value / peak * width)) if value > 0 else 0
    return "█" * filled


# --- data loading -----------------------------------------------------------

def scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def aggregate(items: list) -> dict:
    """Bucket raw index items by their PK/SK shape into the views we render."""
    data = {
        "studies": set(),
        "participants": defaultdict(set),       # study -> {patient}
        "latest_participant": {},               # (study, patient) -> item
        "latest_stream": {},                    # (study, patient, stream) -> item
        "study_daily": defaultdict(dict),       # study -> day -> {count, bytes}
        "stream_totals": defaultdict(lambda: {"count": 0, "bytes": 0}),  # (study, patient, stream)
        "objects": [],                          # per-object dedupe/log records
    }
    for it in items:
        pk, sk = it.get("PK", ""), it.get("SK", "")
        if pk.startswith("OBJ#"):
            data["objects"].append(it)
        elif pk.startswith("STUDY#") and "#P#" in pk and "#S#" in pk and sk.startswith("DAY#"):
            study = pk.split("#")[1]
            patient = pk.split("#P#")[1].split("#S#")[0]
            stream = pk.split("#S#")[1]
            data["studies"].add(study)
            data["participants"][study].add(patient)
            tot = data["stream_totals"][(study, patient, stream)]
            tot["count"] += num(it.get("count")); tot["bytes"] += num(it.get("bytes"))
        elif pk.startswith("STUDY#") and "#P#" not in pk and "#S#" in pk and sk.startswith("DAY#"):
            # Study-level per-stream daily rollup (STUDY#<study>#S#<stream> / DAY#),
            # the bounded-read source for the in-app dashboard. This scan-based tool
            # already derives per-stream totals from the participant-scoped rollups
            # above, so skip these to avoid inventing a bogus "<study>#S#<stream>"
            # study name and double-counting study totals.
            pass
        elif pk.startswith("STUDY#") and "#P#" not in pk and "#S#" not in pk and sk.startswith("DAY#"):
            study = pk.split("#", 1)[1]
            data["studies"].add(study)
            data["study_daily"][study][sk[4:]] = {"count": num(it.get("count")), "bytes": num(it.get("bytes"))}
        elif sk.startswith("LATEST#P#") and "#S#" in sk:
            study = pk.split("#", 1)[1]
            patient = sk[len("LATEST#P#"):].split("#S#")[0]
            stream = sk.split("#S#")[1]
            data["latest_stream"][(study, patient, stream)] = it
            data["studies"].add(study); data["participants"][study].add(patient)
        elif sk.startswith("LATEST#P#"):
            study = pk.split("#", 1)[1]
            patient = sk[len("LATEST#P#"):]
            data["latest_participant"][(study, patient)] = it
            data["studies"].add(study); data["participants"][study].add(patient)
    return data


# --- rendering --------------------------------------------------------------

def hr(char="─", n=64):
    return char * n


def render(data: dict, now: datetime, stale_hours: int):
    studies = sorted(data["studies"])
    n_participants = sum(len(p) for p in data["participants"].values())
    total_uploads = sum(d["count"] for days in data["study_daily"].values() for d in days.values())
    total_bytes = sum(d["bytes"] for days in data["study_daily"].values() for d in days.values())
    all_days = sorted({day for days in data["study_daily"].values() for day in days})
    span = f"{all_days[0]} -> {all_days[-1]}" if all_days else "n/a"
    stale_cutoff = stale_hours * 3600

    print()
    print(f"{C.BOLD}{C.CYAN}  Beiwe Upload Metadata Index{C.RESET}")
    print(f"  {C.DIM}live view -- derived from S3 object-created events, read-only{C.RESET}")
    print(hr("═"))
    print(f"  Studies: {C.BOLD}{len(studies)}{C.RESET}    "
          f"Participants: {C.BOLD}{n_participants}{C.RESET}    "
          f"Uploads: {C.BOLD}{total_uploads}{C.RESET}    "
          f"Volume: {C.BOLD}{human_bytes(total_bytes)}{C.RESET}")
    print(f"  Activity window: {C.DIM}{span}{C.RESET}    "
          f"as of {C.DIM}{now.strftime('%Y-%m-%d %H:%M:%SZ')}{C.RESET}")
    print(hr("═"))

    if not studies:
        print(f"\n  {C.YELLOW}No data in the index yet.{C.RESET} "
              f"Upload something to the raw bucket and re-run.\n")
        return

    # --- recent uploads feed (from per-object records) ---
    feed = sorted(data["objects"], key=lambda o: o.get("upload_time", ""), reverse=True)[:15]
    if feed:
        print(f"\n{C.BOLD}  Recent uploads{C.RESET} {C.DIM}(latest {len(feed)}){C.RESET}")
        for o in feed:
            disp = STREAM_DISPLAY.get(o.get("stream", ""), o.get("stream", "?"))
            print(f"  {C.GREEN}•{C.RESET} {ago(o.get('upload_time', ''), now):>8}  "
                  f"{C.CYAN}{o.get('patient', '?'):<9}{C.RESET} "
                  f"{disp:<15} {C.DIM}{human_bytes(num(o.get('size'))):>9}{C.RESET}")

    # --- per participant: freshness + stale flags ---
    for study in studies:
        print(f"\n{C.BOLD}  Study {C.MAGENTA}{study}{C.RESET}")
        for patient in sorted(data["participants"][study]):
            p_latest = data["latest_participant"].get((study, patient))
            last_iso = p_latest.get("last_upload_time", "") if p_latest else ""
            fresh = (now - parse_time(last_iso)).total_seconds() < stale_cutoff if parse_time(last_iso) else False
            dot = f"{C.GREEN}●{C.RESET}" if fresh else f"{C.RED}○{C.RESET}"
            print(f"    {dot} {C.BOLD}{patient}{C.RESET}  "
                  f"{C.DIM}last upload {ago(last_iso, now)}{C.RESET}")
            streams = sorted(s for (st, pt, s) in data["latest_stream"] if st == study and pt == patient)
            for stream in streams:
                it = data["latest_stream"][(study, patient, stream)]
                tot = data["stream_totals"].get((study, patient, stream), {"count": 0, "bytes": 0})
                disp = STREAM_DISPLAY.get(stream, stream)
                dt = parse_time(it.get("last_upload_time", ""))
                is_stale = dt is None or (now - dt).total_seconds() >= stale_cutoff
                flag = f"  {C.YELLOW}⚠ stale (>{stale_hours}h){C.RESET}" if is_stale else ""
                color = C.YELLOW if is_stale else C.RESET
                print(f"        {color}{disp:<16}{C.RESET} "
                      f"{ago(it.get('last_upload_time', ''), now):>8}  "
                      f"{C.DIM}{tot['count']:>4} files  {human_bytes(tot['bytes']):>9}{C.RESET}{flag}")

    # --- study-level volume over time (bar chart) ---
    for study in studies:
        days = data["study_daily"].get(study, {})
        if not days:
            continue
        print(f"\n{C.BOLD}  Daily upload volume{C.RESET} {C.DIM}-- {study}{C.RESET}")
        peak = max(d["count"] for d in days.values())
        for day in sorted(days):
            d = days[day]
            print(f"    {C.DIM}{day}{C.RESET}  {C.BLUE}{bar(d['count'], peak)}{C.RESET} "
                  f"{d['count']} files  {C.DIM}{human_bytes(d['bytes'])}{C.RESET}")

    print()


# --- entrypoint -------------------------------------------------------------

def resolve_table_name(session, stack: str) -> str:
    cfn = session.client("cloudformation")
    outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == "TableName":
            return o["OutputValue"]
    raise SystemExit(f"No 'TableName' output on stack {stack}; pass --table explicitly.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pretty-print the Upload Metadata Index.")
    ap.add_argument("--profile", default="shiny-dev", help="AWS profile (default: shiny-dev)")
    ap.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    ap.add_argument("--stack", default="MetadataIndexStack", help="CloudFormation stack name")
    ap.add_argument("--table", default=None, help="DynamoDB table name (else resolved from the stack)")
    ap.add_argument("--stale-hours", type=int, default=24, help="Flag streams quiet longer than this (default: 24)")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    args = ap.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        C.disable()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table_name = args.table or resolve_table_name(session, args.stack)
    table = session.resource("dynamodb").Table(table_name)

    items = scan_all(table)
    now = datetime.now(timezone.utc)
    render(aggregate(items), now, args.stale_hours)


if __name__ == "__main__":
    main()
