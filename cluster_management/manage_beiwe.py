#!/usr/bin/env python3
"""
Pause or resume the main Beiwe cost drivers between sessions.

Usage:
  cd cluster_management/
  python manage_beiwe.py pause
  python manage_beiwe.py resume

Pause (in order):
  1. EB auto-scaling group → min/max/desired = 0  (terminates web tier instances)
  2. Processing servers (manager / worker EC2) → stop
  3. RDS instance → stop

Resume (in order, longest first):
  1. RDS → start, wait until available
  2. Processing servers → start
  3. EB ASG → restore saved min/max

Note: the Classic Load Balancer remains running regardless (~$18/month).
Note: AWS auto-restarts stopped RDS instances after 7 days.
"""

import argparse
import json
import sys
from pathlib import Path
from time import sleep

import boto3

CLUSTER_MANAGEMENT_DIR = Path(__file__).resolve().parent
GENERAL_CONFIG = CLUSTER_MANAGEMENT_DIR / "general_configuration" / "global_configuration.json"
SETUP_CONFIG   = CLUSTER_MANAGEMENT_DIR / "general_configuration" / "setup_config.json"
ENV_CONFIG_DIR = CLUSTER_MANAGEMENT_DIR / "environment_configuration"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_region() -> str:
    with open(GENERAL_CONFIG) as f:
        return json.load(f)["AWS_REGION"]


def get_env_name(override: str | None) -> str:
    if override:
        return override
    if SETUP_CONFIG.exists():
        with open(SETUP_CONFIG) as f:
            return json.load(f)["env_name"]
    raise SystemExit("Could not determine environment name. Pass --env-name or create setup_config.json.")


def state_file(env_name: str) -> Path:
    return ENV_CONFIG_DIR / f"{env_name}_suspend_state.json"


# ---------------------------------------------------------------------------
# EB / ASG
# ---------------------------------------------------------------------------

def get_eb_asg_name(region: str, env_name: str) -> str:
    eb = boto3.client("elasticbeanstalk", region_name=region)
    resources = eb.describe_environment_resources(EnvironmentName=env_name)
    asgs = resources["EnvironmentResources"]["AutoScalingGroups"]
    if not asgs:
        raise RuntimeError(f"No ASG found for EB environment '{env_name}'")
    return asgs[0]["Name"]


def get_asg_settings(region: str, asg_name: str) -> dict:
    asg = boto3.client("autoscaling", region_name=region)
    groups = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
    if not groups:
        raise RuntimeError(f"ASG '{asg_name}' not found")
    g = groups[0]
    return {"MinSize": g["MinSize"], "MaxSize": g["MaxSize"], "DesiredCapacity": g["DesiredCapacity"]}


def set_asg_capacity(region: str, asg_name: str, min_size: int, max_size: int, desired: int) -> None:
    asg = boto3.client("autoscaling", region_name=region)
    asg.update_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MinSize=min_size,
        MaxSize=max_size,
        DesiredCapacity=desired,
    )


# ---------------------------------------------------------------------------
# EC2 processing servers
# ---------------------------------------------------------------------------

def get_processing_instances(region: str, env_name: str) -> list[dict]:
    ec2 = boto3.client("ec2", region_name=region)
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [
                f"{env_name} data processing manager",
                f"{env_name} data processing server",
            ]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )["Reservations"]
    return [i for r in reservations for i in r["Instances"]]


def stop_instances(region: str, instance_ids: list[str]) -> None:
    boto3.client("ec2", region_name=region).stop_instances(InstanceIds=instance_ids)


def start_instances(region: str, instance_ids: list[str]) -> None:
    boto3.client("ec2", region_name=region).start_instances(InstanceIds=instance_ids)


# ---------------------------------------------------------------------------
# RDS
# ---------------------------------------------------------------------------

def get_rds_identifier(env_name: str) -> str:
    return f"{env_name}-database"


def get_rds_status(region: str, db_id: str) -> str:
    rds = boto3.client("rds", region_name=region)
    return rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]["DBInstanceStatus"]


def stop_rds(region: str, db_id: str) -> None:
    rds = boto3.client("rds", region_name=region)
    status = get_rds_status(region, db_id)
    if status == "stopped":
        print(f"  RDS {db_id}: already stopped")
        return
    if status != "available":
        print(f"  RDS {db_id}: in state '{status}', skipping stop")
        return
    rds.stop_db_instance(DBInstanceIdentifier=db_id)
    print(f"  RDS {db_id}: stop requested (takes a few minutes)")


def start_rds(region: str, db_id: str) -> None:
    rds = boto3.client("rds", region_name=region)
    status = get_rds_status(region, db_id)
    if status == "available":
        print(f"  RDS {db_id}: already running")
        return
    if status != "stopped":
        print(f"  RDS {db_id}: in state '{status}', skipping start")
        return
    rds.start_db_instance(DBInstanceIdentifier=db_id)
    print(f"  RDS {db_id}: starting...", end="", flush=True)
    for _ in range(60):
        sleep(15)
        s = get_rds_status(region, db_id)
        print(".", end="", flush=True)
        if s == "available":
            print(" ready.")
            return
    print()
    print(f"  Warning: timed out waiting for RDS to become available. Check the console.")


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------

def do_pause(region: str, env_name: str) -> None:
    print(f"\nPausing Beiwe environment '{env_name}' in {region}...\n")
    db_id = get_rds_identifier(env_name)

    # Save current ASG settings so resume can restore them
    asg_name = get_eb_asg_name(region, env_name)
    current = get_asg_settings(region, asg_name)
    saved = {"asg_name": asg_name, **current}
    state_file(env_name).write_text(json.dumps(saved, indent=2))
    print(f"  Saved ASG state: MinSize={current['MinSize']}, MaxSize={current['MaxSize']}")

    # 1. EB ASG → 0
    print(f"  EB ASG '{asg_name}': setting to 0...")
    set_asg_capacity(region, asg_name, 0, 0, 0)
    print("  EB ASG set to 0 (instances will terminate shortly).")

    # 2. Processing servers
    instances = get_processing_instances(region, env_name)
    if instances:
        running = [i["InstanceId"] for i in instances if i["State"]["Name"] == "running"]
        if running:
            print(f"  Stopping processing servers: {', '.join(running)}")
            stop_instances(region, running)
        else:
            print("  Processing servers already stopped.")
    else:
        print("  No processing servers found.")

    # 3. RDS
    stop_rds(region, db_id)

    sf = state_file(env_name)
    print(f"\nPaused. State saved to {sf.name}.")
    print("Note: the load balancer keeps running (~$18/month regardless).")
    print("Note: AWS auto-restarts stopped RDS instances after 7 days.")


def do_resume(region: str, env_name: str) -> None:
    print(f"\nResuming Beiwe environment '{env_name}' in {region}...\n")
    db_id = get_rds_identifier(env_name)

    # 1. RDS — start first; it takes the longest
    start_rds(region, db_id)

    # 2. Processing servers
    instances = get_processing_instances(region, env_name)
    if instances:
        stopped = [i["InstanceId"] for i in instances if i["State"]["Name"] == "stopped"]
        if stopped:
            print(f"  Starting processing servers: {', '.join(stopped)}")
            start_instances(region, stopped)
        else:
            print("  Processing servers already running.")
    else:
        print("  No processing servers found.")

    # 3. EB ASG — restore saved settings
    sf = state_file(env_name)
    if sf.exists():
        saved = json.loads(sf.read_text())
        asg_name = saved["asg_name"]
        min_size = saved.get("MinSize", 1)
        max_size = saved.get("MaxSize", 4)
        desired  = saved.get("DesiredCapacity", 1)
    else:
        print("  No saved ASG state found — using defaults (min=1, max=4, desired=1).")
        asg_name = get_eb_asg_name(region, env_name)
        min_size, max_size, desired = 1, 4, 1

    print(f"  EB ASG '{asg_name}': restoring to MinSize={min_size}, MaxSize={max_size}, Desired={desired}...")
    set_asg_capacity(region, asg_name, min_size, max_size, desired)
    print("  EB ASG restored (new instances will launch shortly).")

    print("\nResumed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Pause or resume the Beiwe cluster cost drivers.")
    parser.add_argument("command", choices=["pause", "resume"])
    parser.add_argument("--env-name", help="EB environment name (default: read from setup_config.json)")
    parser.add_argument("--region",   help="AWS region (default: read from global_configuration.json)")
    args = parser.parse_args()

    region   = args.region   or get_region()
    env_name = get_env_name(args.env_name)

    if args.command == "pause":
        do_pause(region, env_name)
    else:
        do_resume(region, env_name)


if __name__ == "__main__":
    main()
