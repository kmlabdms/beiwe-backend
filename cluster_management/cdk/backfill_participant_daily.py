#!/usr/bin/env python3
"""Backfill the sharded participant-daily aggregate + first-seen pointers from the
per-(participant,stream) rollups already in the table -- WITHOUT wiping it.

Derivation: the participant-daily matrix is the per-day SUM (across streams) of the
existing STUDY#<study>#P#<patient>#S#<stream> / DAY# rollups; first-seen is the MIN
DAY# per participant. Writes are SET (absolute), so the job is idempotent and
re-runnable.

SAFE ONLY WHILE INGESTION IS PAUSED. The live writer uses atomic ADD; a SET racing
an ADD corrupts the value. This script therefore refuses to --apply unless the
EventBridge rule is DISABLED and the SQS queue + DLQ are drained to empty. The deploy
helper (deploy_metadata_index.sh backfill) orchestrates disable -> drain-wait ->
backfill -> re-enable. NOTE: drain by WAITING (the live writer finishes queued
uploads into the rollups), never by purging -- purging a live table drops uploads.

Admin tooling: this Scans the table and writes, so it needs admin/deploy creds, NOT
the least-privilege reader role. It does not touch the dashboard read path.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import boto3

# Import the writer's shard fn so backfilled items land in the SAME shard the live
# writer uses (and reuse its SHARDS constant). Mirrors test_show_metadata_index.py's
# path insert.
_LAMBDA_DIR = Path(__file__).resolve().parent / "lambdas" / "metadata_index"
if str(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_DIR))
from dynamo_writer import SHARDS, _shard_for  # noqa: E402


def aggregate_rollups(items, study_filter=None):
    """Pure: per-(participant,stream) DAY# rollup items -> (daily, first).

    daily: {(study, patient, day): {"count", "bytes"}} summed across streams;
    first: {(study, patient): earliest_day}. Items that are not per-(participant,
    stream) DAY# rollups are ignored, so a raw table Scan can be passed in directly.
    """
    daily = defaultdict(lambda: {"count": 0, "bytes": 0})
    first = {}
    for it in items:
        pk, sk = it.get("PK", ""), it.get("SK", "")
        if "#P#" not in pk or "#S#" not in pk or not sk.startswith("DAY#"):
            continue
        rest = pk[len("STUDY#"):] if pk.startswith("STUDY#") else pk
        study = rest.split("#P#", 1)[0]
        patient = rest.split("#P#", 1)[1].split("#S#", 1)[0]
        if study_filter and study not in study_filter:
            continue
        day = sk[len("DAY#"):]
        cell = daily[(study, patient, day)]
        cell["count"] += int(it.get("count", 0) or 0)
        cell["bytes"] += int(it.get("bytes", 0) or 0)
        k = (study, patient)
        if k not in first or day < first[k]:
            first[k] = day
    return daily, first


def _scan(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _stack_outputs(session, region, stack):
    cfn = session.client("cloudformation", region_name=region)
    outs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def _assert_paused(session, region, outputs):
    """Refuse to write unless ingestion is verifiably paused: rule DISABLED and both
    queues drained. This is the load-bearing safety check (SET must not race ADD)."""
    events = session.client("events", region_name=region)
    state = events.describe_rule(Name=outputs["EventBridgeRuleName"])["State"]
    if state != "DISABLED":
        raise SystemExit(f"refusing: EventBridge rule is {state}, not DISABLED -- pause ingestion first")
    sqs = session.client("sqs", region_name=region)
    for url in (outputs["QueueUrl"], outputs["DlqUrl"]):
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        pending = int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])
        if pending:
            raise SystemExit(f"refusing: queue not drained ({pending} messages pending) -- wait for the drain")


def _write(table, daily, first):
    """SET (absolute) the derived items + ensure CONFIG/SHARDS. put_item overwrites."""
    with table.batch_writer() as batch:
        for (study, patient, day), agg in daily.items():
            batch.put_item(Item={
                "PK": f"STUDY#{study}#DAILY#{_shard_for(patient)}",
                "SK": f"P#{patient}#DAY#{day}",
                "count": agg["count"], "bytes": agg["bytes"],
            })
        for (study, patient), fd in first.items():
            batch.put_item(Item={
                "PK": f"STUDY#{study}", "SK": f"FIRST#P#{patient}", "first_day": fd,
            })
    table.put_item(Item={"PK": "CONFIG", "SK": "SHARDS", "value": SHARDS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=None, help="AWS profile (default: credential chain)")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--stack", default="MetadataIndexStack")
    ap.add_argument("--table", default=None, help="table name (else resolved from the stack)")
    ap.add_argument("--study", action="append", help="limit to this study object_id (repeatable)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (requires the rule DISABLED and queues drained)")
    args = ap.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    outputs = _stack_outputs(session, args.region, args.stack)
    table = session.resource("dynamodb", region_name=args.region).Table(args.table or outputs["TableName"])

    daily, first = aggregate_rollups(_scan(table), set(args.study) if args.study else None)
    print(f"derived {len(daily)} participant-day cells across {len(first)} participants (SHARDS={SHARDS})")
    if not args.apply:
        print("(dry-run; pass --apply to write -- requires ingestion paused: rule DISABLED + queues drained)")
        return
    _assert_paused(session, args.region, outputs)
    _write(table, daily, first)
    print("backfill complete.")


if __name__ == "__main__":
    main()
