#!/usr/bin/env python3
"""
Beiwe cluster deployment orchestrator.

Automates every step of the Beiwe Scalable Deployment wiki that can be
automated, while still delegating to launch_script.py for the steps it owns.

Usage (run from anywhere inside the repo):
  cd cluster_management/
  python setup_beiwe.py [options]

What this script does:
  1.  cdk deploy  — creates IAM user + EC2 key pair (wiki steps 3-4)
  2.  Reads credentials/key from AWS and writes config JSON files
  3.  pip install launch_requirements.txt
  4.  launch_script.py -help-setup-new-environment
  5.  Writes domain + Sentry DSNs into environment config files
  6.  launch_script.py -create-environment
  7.  launch_script.py -create-manager
  8.  launch_script.py -create-worker  (optional)
  9.  Writes .elasticbeanstalk/config.yml and eb-cli AWS profile
  10. Requests ACM certificate; pauses for DNS validation
  11. Configures load balancer HTTPS:443 listener
  12. eb deploy

What stays manual (cannot be automated):
  - Creating the AWS account
  - Acquiring a domain name
  - Creating a Sentry account and projects
  - Adding the ACM DNS validation CNAME at your registrar
  - Pointing your domain's CNAME/ALIAS to the load balancer
  - Changing the default admin password after first login
"""

import argparse
import configparser
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from time import sleep

import boto3

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CLUSTER_MANAGEMENT_DIR = Path(__file__).resolve().parent
CDK_DIR = CLUSTER_MANAGEMENT_DIR / "cdk"
GENERAL_CONFIG_DIR = CLUSTER_MANAGEMENT_DIR / "general_configuration"
ENV_CONFIG_DIR = CLUSTER_MANAGEMENT_DIR / "environment_configuration"
REPO_ROOT = CLUSTER_MANAGEMENT_DIR.parent
LAUNCH_SCRIPT = CLUSTER_MANAGEMENT_DIR / "launch_script.py"

STACK_NAME = "BeiwePrerequisitesStack"
DUMMY_DSN = "https://XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX@sentry.io/0"


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def banner(step: int, total: int, text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  [{step}/{total}] {text}")
    print(f"{'─' * 60}\n")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  (required — please enter a value)")


def confirm(prompt: str) -> None:
    input(f"  {prompt}  [press Enter to continue] ")


# ---------------------------------------------------------------------------
# CDK helpers
# ---------------------------------------------------------------------------

def deploy_cdk(region: str) -> None:
    subprocess.run(
        [
            "cdk", "deploy",
            "--require-approval=never",
            "--context", f"region={region}",
        ],
        cwd=str(CDK_DIR),
        check=True,
        env={**os.environ, "CDK_DEFAULT_REGION": region},
    )


def get_stack_outputs(region: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=region)
    resp = cf.describe_stacks(StackName=STACK_NAME)
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in resp["Stacks"][0]["Outputs"]
    }


# ---------------------------------------------------------------------------
# Key pair / credentials helpers
# ---------------------------------------------------------------------------

def download_private_key(region: str, key_pair_id: str, dest: Path) -> None:
    """Retrieve EC2 private key from SSM Parameter Store and save it."""
    ssm = boto3.client("ssm", region_name=region)
    param_name = f"/ec2/keypair/{key_pair_id}"
    param = ssm.get_parameter(Name=param_name, WithDecryption=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(param["Parameter"]["Value"])
    dest.chmod(0o600)
    print(f"  Private key → {dest}")


def get_iam_credentials(region: str, secret_arn: str) -> dict[str, str]:
    sm = boto3.client("secretsmanager", region_name=region)
    secret = sm.get_secret_value(SecretId=secret_arn)
    return json.loads(secret["SecretString"])



# ---------------------------------------------------------------------------
# Config file writers
# ---------------------------------------------------------------------------

def write_aws_credentials(access_key_id: str, secret_access_key: str) -> None:
    path = GENERAL_CONFIG_DIR / "aws_credentials.json"
    path.write_text(json.dumps({
        "AWS_ACCESS_KEY_ID": access_key_id,
        "AWS_SECRET_ACCESS_KEY": secret_access_key,
    }, indent=2))
    print(f"  Wrote {path.relative_to(CLUSTER_MANAGEMENT_DIR)}")


def write_global_configuration(
    key_name: str, key_path: Path, vpc_id: str, region: str, admin_email: str
) -> None:
    path = GENERAL_CONFIG_DIR / "global_configuration.json"
    path.write_text(json.dumps({
        "DEPLOYMENT_KEY_NAME": key_name,
        "DEPLOYMENT_KEY_FILE_PATH": str(key_path),
        "VPC_ID": vpc_id,
        "AWS_REGION": region,
        "SYSTEM_ADMINISTRATOR_EMAIL": admin_email,
    }, indent=2))
    print(f"  Wrote {path.relative_to(CLUSTER_MANAGEMENT_DIR)}")


def write_environment_variables_file(
    env_name: str,
    domain: str,
    sentry_eb_dsn: str,
    sentry_dp_dsn: str,
    sentry_js_dsn: str,
) -> None:
    path = ENV_CONFIG_DIR / f"{env_name}_beiwe_environment_variables.json"
    path.write_text(json.dumps({
        "DOMAIN": domain,
        "SENTRY_ELASTIC_BEANSTALK_DSN": sentry_eb_dsn,
        "SENTRY_DATA_PROCESSING_DSN": sentry_dp_dsn,
        "SENTRY_JAVASCRIPT_DSN": sentry_js_dsn,
    }, indent=1))
    print(f"  Wrote {path.name}")


def write_elasticbeanstalk_config(env_name: str, region: str, key_name: str) -> None:
    config_dir = REPO_ROOT / ".elasticbeanstalk"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yml").write_text(textwrap.dedent(f"""\
        branch-defaults:
          main:
            environment: {env_name}
        global:
          application_name: beiwe-application
          default_ec2_keyname: {key_name}
          default_region: {region}
          profile: eb-cli
          sc: git
    """))
    print("  Wrote .elasticbeanstalk/config.yml")


def write_aws_profile(
    access_key_id: str, secret_access_key: str, region: str, profile: str = "eb-cli"
) -> None:
    aws_dir = Path.home() / ".aws"
    aws_dir.mkdir(exist_ok=True)

    creds_path = aws_dir / "credentials"
    if not creds_path.exists():
        creds_path.touch()
    creds = configparser.ConfigParser()
    creds.read(creds_path)
    if profile not in creds:
        creds[profile] = {}
    creds[profile]["aws_access_key_id"] = access_key_id
    creds[profile]["aws_secret_access_key"] = secret_access_key
    with open(creds_path, "w") as f:
        creds.write(f)

    config_path = aws_dir / "config"
    if not config_path.exists():
        config_path.touch()
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    section = f"profile {profile}"
    if section not in cfg:
        cfg[section] = {}
    cfg[section]["region"] = region
    with open(config_path, "w") as f:
        cfg.write(f)

    print(f"  AWS profile '{profile}' written to ~/.aws/")


# ---------------------------------------------------------------------------
# launch_script.py wrapper
# ---------------------------------------------------------------------------

def run_launch_script(arg: str, env_name: str) -> None:
    """
    Run launch_script.py, feeding env_name to its interactive stdin prompt.
    All commands take exactly one environment name as their only interactive
    input, so a single newline-terminated string covers all cases.
    """
    subprocess.run(
        [sys.executable, str(LAUNCH_SCRIPT), arg],
        cwd=str(CLUSTER_MANAGEMENT_DIR),
        input=(env_name + "\n").encode(),
        check=True,
    )


# ---------------------------------------------------------------------------
# ACM certificate helpers
# ---------------------------------------------------------------------------

def request_acm_certificate(region: str, domain: str) -> str:
    acm = boto3.client("acm", region_name=region)
    resp = acm.request_certificate(
        DomainName=domain,
        ValidationMethod="DNS",
        SubjectAlternativeNames=[f"www.{domain}"],
        Options={"CertificateTransparencyLoggingPreference": "ENABLED"},
    )
    cert_arn = resp["CertificateArn"]
    print(f"  Certificate ARN: {cert_arn}")
    return cert_arn


def get_acm_validation_records(region: str, cert_arn: str) -> list[dict]:
    """Poll ACM until DNS validation CNAME records are available (usually <30 s)."""
    acm = boto3.client("acm", region_name=region)
    for _ in range(30):
        resp = acm.describe_certificate(CertificateArn=cert_arn)
        options = resp["Certificate"].get("DomainValidationOptions", [])
        records = [o["ResourceRecord"] for o in options if "ResourceRecord" in o]
        if records:
            return records
        sleep(5)
    raise RuntimeError("Timed out waiting for ACM to produce validation DNS records.")


def wait_for_cert_issued(region: str, cert_arn: str) -> None:
    acm = boto3.client("acm", region_name=region)
    print("  Waiting for certificate validation", end="", flush=True)
    for _ in range(120):  # up to 20 minutes
        status = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]["Status"]
        if status == "ISSUED":
            print(" ✓")
            return
        if status in ("FAILED", "REVOKED", "INACTIVE"):
            raise RuntimeError(f"ACM certificate entered unexpected status: {status}")
        print(".", end="", flush=True)
        sleep(10)
    raise RuntimeError("Timed out waiting for ACM certificate to be issued (20 min).")


# ---------------------------------------------------------------------------
# Load balancer helpers
# ---------------------------------------------------------------------------

def get_eb_classic_lb_name(region: str, env_name: str) -> str:
    eb = boto3.client("elasticbeanstalk", region_name=region)
    resources = eb.describe_environment_resources(EnvironmentName=env_name)
    lbs = resources["EnvironmentResources"]["LoadBalancers"]
    if not lbs:
        raise RuntimeError(f"No load balancer found for EB environment '{env_name}'.")
    lb_name = lbs[0]["Name"]
    print(f"  Load balancer: {lb_name}")
    return lb_name


def configure_classic_lb_https(region: str, lb_name: str, cert_arn: str) -> None:
    """Add HTTPS:443 → instance:443 listener to Classic Load Balancer."""
    elb = boto3.client("elb", region_name=region)
    resp = elb.describe_load_balancers(LoadBalancerNames=[lb_name])
    existing_ports = {
        ld["Listener"]["LoadBalancerPort"]
        for ld in resp["LoadBalancerDescriptions"][0]["ListenerDescriptions"]
    }

    if 443 not in existing_ports:
        print("  Adding HTTPS:443 listener...")
        elb.create_load_balancer_listeners(
            LoadBalancerName=lb_name,
            Listeners=[{
                "Protocol": "HTTPS",
                "LoadBalancerPort": 443,
                "InstanceProtocol": "HTTPS",
                "InstancePort": 443,
                "SSLCertificateId": cert_arn,
            }],
        )
    else:
        print("  Updating HTTPS:443 listener certificate...")
        elb.set_load_balancer_listener_ssl_certificate(
            LoadBalancerName=lb_name,
            LoadBalancerPort=443,
            SSLCertificateId=cert_arn,
        )
    print("  HTTPS listener configured.")


def get_classic_lb_dns(region: str, lb_name: str) -> str:
    elb = boto3.client("elb", region_name=region)
    resp = elb.describe_load_balancers(LoadBalancerNames=[lb_name])
    return resp["LoadBalancerDescriptions"][0]["DNSName"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Beiwe cluster deployment orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            All options can be supplied interactively if omitted.
            The script is designed for a fresh deployment. Re-running a
            partial deployment is supported via --skip-cdk and by answering
            the environment-name prompt with the same name as before.
        """),
    )
    parser.add_argument("--region", help="AWS region (e.g. us-east-2)")
    parser.add_argument("--env-name", help="EB environment name (4–40 chars, letters/numbers/hyphens)")
    parser.add_argument("--admin-email", help="Email for AWS operational alerts")
    parser.add_argument("--domain", help="Beiwe domain (e.g. beiwe.mylab.edu)")
    parser.add_argument("--sentry-eb-dsn", default=DUMMY_DSN, help="Sentry DSN for Elastic Beanstalk errors")
    parser.add_argument("--sentry-dp-dsn", default=DUMMY_DSN, help="Sentry DSN for data-processing errors")
    parser.add_argument("--sentry-js-dsn", default=DUMMY_DSN, help="Sentry public DSN for JavaScript errors")
    parser.add_argument("--create-worker", action="store_true", help="Also create a worker server")
    parser.add_argument("--skip-cdk", action="store_true",
                        help="Skip cdk deploy (BeiwePrerequisitesStack already deployed)")
    args = parser.parse_args()

    # ── Gather required inputs ──────────────────────────────────────────────
    region = args.region or ask("AWS region (e.g. us-east-2)")
    env_name = args.env_name or ask("EB environment name (4–40 chars, letters/numbers/hyphens)")
    admin_email = args.admin_email or ask("Administrator email for AWS alerts")
    domain = args.domain or ask("Beiwe domain name (e.g. beiwe.mylab.edu)")

    sentry_eb_dsn = args.sentry_eb_dsn
    sentry_dp_dsn = args.sentry_dp_dsn
    sentry_js_dsn = args.sentry_js_dsn
    if sentry_eb_dsn == DUMMY_DSN:
        print("\nSentry DSNs are optional but strongly recommended for error monitoring.")
        print("Press Enter to use placeholder values (can be updated later).")
        sentry_eb_dsn = ask("Sentry EB/data-processing DSN", default=DUMMY_DSN)
        sentry_dp_dsn = ask("Sentry data-processing DSN", default=sentry_eb_dsn)
        sentry_js_dsn = ask("Sentry JavaScript (public) DSN", default=sentry_eb_dsn)

    if not args.create_worker:
        ans = ask("\nCreate a worker server? (y/N)", default="N").lower()
        create_worker = ans in ("y", "yes")
    else:
        create_worker = True

    total = 12 + (1 if create_worker else 0)
    step = 0

    def next_step(label: str) -> None:
        nonlocal step
        step += 1
        banner(step, total, label)

    # ── 1. CDK deploy ───────────────────────────────────────────────────────
    next_step("Deploy CDK prerequisites stack (IAM user, EC2 key pair, default VPC)")
    if args.skip_cdk:
        print("  --skip-cdk: reading existing stack outputs.")
    else:
        print("  This creates the beiwe-deploy IAM user and EC2 key pair.")
        deploy_cdk(region)

    # ── 2. Read CDK outputs ─────────────────────────────────────────────────
    next_step("Read CDK stack outputs")
    outputs = get_stack_outputs(region)
    key_name = outputs["KeyPairName"]
    key_pair_id = outputs["KeyPairId"]
    credentials_secret_arn = outputs["CredentialsSecretArn"]
    vpc_id = outputs["VpcId"]
    print(f"  Key pair name:   {key_name}")
    print(f"  Credentials ARN: {credentials_secret_arn}")
    print(f"  VPC ID:          {vpc_id}")

    # ── 3. Download private key ─────────────────────────────────────────────
    next_step("Download EC2 private key from SSM Parameter Store")
    key_path = Path.home() / ".ssh" / f"{key_name}.pem"
    if key_path.exists():
        print(f"  {key_path} already exists, skipping download.")
    else:
        download_private_key(region, key_pair_id, key_path)

    # ── 4. Write config files ───────────────────────────────────────────────
    next_step("Write aws_credentials.json and global_configuration.json")
    credentials = get_iam_credentials(region, credentials_secret_arn)
    write_aws_credentials(
        credentials["AWS_ACCESS_KEY_ID"], credentials["AWS_SECRET_ACCESS_KEY"]
    )
    write_global_configuration(key_name, key_path, vpc_id, region, admin_email)

    # ── 5. Install launch requirements ──────────────────────────────────────
    next_step("pip install launch_requirements.txt")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "launch_requirements.txt"],
        cwd=str(CLUSTER_MANAGEMENT_DIR),
        check=True,
    )

    # ── 6. Create environment config files ──────────────────────────────────
    next_step("Create environment configuration files (launch_script.py -help-setup-new-environment)")
    env_vars_file = ENV_CONFIG_DIR / f"{env_name}_beiwe_environment_variables.json"
    if env_vars_file.exists():
        print(f"  {env_vars_file.name} already exists; overwriting with provided values.")
    else:
        run_launch_script("-help-setup-new-environment", env_name)
    write_environment_variables_file(env_name, domain, sentry_eb_dsn, sentry_dp_dsn, sentry_js_dsn)

    # ── 7. Create EB environment + RDS ──────────────────────────────────────
    next_step("Create Elastic Beanstalk environment and RDS instance (launch_script.py -create-environment)")
    print("  This takes 5–15 minutes.")
    run_launch_script("-create-environment", env_name)

    # ── 8. Create manager server ─────────────────────────────────────────────
    next_step("Create manager (RabbitMQ + Celery) server (launch_script.py -create-manager)")
    print("  This takes 5–10 minutes.")
    run_launch_script("-create-manager", env_name)

    # ── 9. (Optional) Create worker server ──────────────────────────────────
    if create_worker:
        next_step("Create worker server (launch_script.py -create-worker)")
        run_launch_script("-create-worker", env_name)

    # ── N. Write .elasticbeanstalk/config.yml + AWS profile ─────────────────
    next_step("Write .elasticbeanstalk/config.yml and 'eb-cli' AWS credentials profile")
    write_elasticbeanstalk_config(env_name, region, key_name)
    write_aws_profile(
        credentials["AWS_ACCESS_KEY_ID"], credentials["AWS_SECRET_ACCESS_KEY"], region
    )

    # ── N+1. Request ACM certificate ─────────────────────────────────────────
    next_step(f"Request ACM certificate for {domain}")
    cert_arn = request_acm_certificate(region, domain)

    print("\n  Retrieving DNS validation records...")
    dns_records = get_acm_validation_records(region, cert_arn)
    print(f"\n  Add the following CNAME record at your DNS registrar:\n")
    for record in dns_records:
        print(f"    Name:  {record['Name']}")
        print(f"    Type:  CNAME")
        print(f"    Value: {record['Value']}")
    print()
    confirm("Add the CNAME record above, then press Enter to wait for validation.")
    wait_for_cert_issued(region, cert_arn)

    # ── N+2. Configure HTTPS on load balancer ───────────────────────────────
    next_step("Configure HTTPS:443 listener on load balancer")
    lb_name = get_eb_classic_lb_name(region, env_name)
    configure_classic_lb_https(region, lb_name, cert_arn)
    lb_dns = get_classic_lb_dns(region, lb_name)

    print(f"\n  Load balancer DNS: {lb_dns}")
    print(f"\n  ACTION REQUIRED — point '{domain}' at the load balancer:")
    print(f"    Subdomain: CNAME  {domain} → {lb_dns}")
    print(f"    Root domain: use Route 53 ALIAS record (most DNS providers don't support ALIAS for roots)")
    print()
    confirm("Add the DNS record above, then press Enter when ready to deploy.")

    # ── N+3. eb deploy ───────────────────────────────────────────────────────
    next_step("Deploy application (eb deploy)")
    subprocess.run(["eb", "deploy"], cwd=str(REPO_ROOT), check=True)

    # ── Done ─────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  DEPLOYMENT COMPLETE")
    print(f"{'═' * 60}")
    print(f"\n  https://{domain}")
    print(f"\n  First login:")
    print("    Username: default_admin")
    print("    Password: abcABC123!@#")
    print("  Change this password immediately after logging in.\n")


if __name__ == "__main__":
    main()
