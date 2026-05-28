# Beiwe Cluster Deployment

This directory contains two complementary tools for deploying a Beiwe cluster:

- **`setup_beiwe.py`** — an orchestration script that runs the full deployment end-to-end, calling `launch_script.py` for the steps it owns
- **`cdk/`** — a CDK app that provisions the AWS prerequisites (`launch_script.py` cannot create these itself)

The authoritative reference is the [Beiwe Scalable Deployment wiki](https://github.com/onnela-lab/beiwe-backend/wiki/Deployment-Instructions---Scalable-Deployment). This tooling automates everything it can while leaving `launch_script.py` in charge of the steps it was built for.

---

## Prerequisites

Before running `setup_beiwe.py` you need:

1. **An AWS account** with credentials for a human/operator identity (root or an existing admin user). These are only used to deploy the CDK stack; after that, the `beiwe-deploy` IAM user created by CDK takes over.

2. **A domain name** you control (e.g. `beiwe.mylab.edu`). You need to be able to add DNS records during the deployment.

3. **Python 3.10+** with a virtual environment (strongly recommended).

4. **Node.js** (required by the CDK CLI).

5. **AWS CDK CLI** — install once globally:
   ```
   npm install -g aws-cdk
   ```

6. **EB CLI** — must be installed in isolation because `launch_requirements.txt` installs `fabric3` (Fabric 1.x), which conflicts with the EB CLI's dependency on Fabric 2.x. Use `pipx`, which gives each tool its own environment:
   ```
   pipx install awsebcli
   ```
   Do **not** install `awsebcli` with plain `pip install` into the same virtualenv where you run `launch_script.py` or `setup_beiwe.py`.

7. **AWS CLI** configured with operator credentials for the target account and region:
   ```
   aws configure
   ```
   The operator credentials are used only for `cdk deploy`. After that, `setup_beiwe.py` reads the newly created `beiwe-deploy` key from Secrets Manager and uses that for everything else.

8. **CDK bootstrap** — if this account/region has never been used with CDK:
   ```
   cdk bootstrap aws://ACCOUNT_ID/REGION
   ```

9. **Sentry account** (optional but recommended) — create a project at [sentry.io](https://sentry.io) and collect DSN values before running the script. You can use placeholder values and update them later.

---

## Automated deployment

Install the CDK dependencies:
```
pip install -r cluster_management/cdk/requirements.txt
```

Then run the orchestrator from anywhere in the repo:
```
cd cluster_management/
python setup_beiwe.py
```

The script will prompt for any required values it wasn't given as flags. You can also supply everything up front:
```
python setup_beiwe.py \
  --region       us-east-2 \
  --env-name     my-beiwe \
  --admin-email  ops@mylab.edu \
  --domain       beiwe.mylab.edu \
  --create-worker
```

Run `python setup_beiwe.py --help` for the full option list.

### What the script does

| Step | What happens |
|------|-------------|
| 1 | `cdk deploy` — creates IAM user `beiwe-deploy`, EC2 key pair, and default VPC (if none exists) |
| 2 | Reads credentials, key pair details, and VPC ID from AWS (Secrets Manager / SSM / CFn outputs) |
| 3 | Downloads the private key to `~/.ssh/beiwe-deployment-key.pem` |
| 4 | Writes `general_configuration/aws_credentials.json` and `global_configuration.json` |
| 5 | `pip install launch_requirements.txt` |
| 6 | `launch_script.py -help-setup-new-environment` then writes domain + Sentry DSNs |
| 7 | `launch_script.py -create-environment` (EB environment + RDS, ~10 min) |
| 8 | `launch_script.py -create-manager` (RabbitMQ + Celery manager server, ~10 min) |
| 9 | `launch_script.py -create-worker` (optional, pass `--create-worker`) |
| 10 | Writes `.elasticbeanstalk/config.yml` and the `eb-cli` AWS credentials profile |
| 11 | Requests ACM certificate; **pauses** so you can add the DNS validation CNAME |
| 12 | Configures HTTPS:443 listener on the Classic Load Balancer |
| 13 | **Pauses** so you can point your domain at the load balancer |
| 14 | `eb deploy` |

### What you still do manually

| Step | Why it can't be automated |
|------|--------------------------|
| Create AWS account | External |
| Acquire domain name | External |
| Create Sentry account/project | External service |
| Add ACM DNS validation CNAME | Requires access to your DNS registrar |
| Point domain at load balancer | DNS propagation requires your registrar |
| Change the default admin password | Must be done by a human after first login |

---

## Re-running a partial deployment

The script is designed for a fresh deployment. If something fails partway through:

- Use `--skip-cdk` to skip the CDK deploy step if the `BeiwePrerequisitesStack` stack already exists.
- The script will not re-download the private key if `~/.ssh/beiwe-deployment-key.pem` already exists.
- If the environment config files already exist, they will be overwritten with the values you provide.
- For failures inside `launch_script.py` steps, you can re-run `launch_script.py` directly (the script is idempotent for most operations) and then re-run `setup_beiwe.py --skip-cdk` to continue.

---

## Updating an existing deployment

Use `launch_script.py` and `eb deploy` directly — `setup_beiwe.py` is for initial provisioning only.

```
git pull
cd cluster_management/
eb deploy    # from the repo root, or run from there
```

To replace processing servers after a code or architecture change:
```
python launch_script.py -terminate-processing-servers
python launch_script.py -create-manager
python launch_script.py -create-worker   # if applicable
```

---

## File reference

```
cluster_management/
  setup_beiwe.py                   — automated deployment orchestrator (this is the entry point)
  launch_script.py                 — original deployment CLI; called by setup_beiwe.py
  launch_requirements.txt          — pip dependencies for launch_script.py
  example_helper.sh                — example of how to populate config files manually

  cdk/
    app.py                         — CDK app entry point
    prerequisites_stack.py         — IAM user + EC2 key pair stack
    cdk.json                       — CDK configuration
    requirements.txt               — aws-cdk-lib, constructs

  general_configuration/           — shared config files (gitignored after population)
    aws_credentials.example.json   — template for aws_credentials.json
    global_configuration.example.json — template for global_configuration.json
    beiwe_automation_policy.json   — IAM policy attached to beiwe-deploy user
    beiwe_server_aws_access.json   — IAM policy for EB instance profile

  environment_configuration/       — per-environment config files (gitignored)
    {env}_beiwe_environment_variables.json  — domain, Sentry DSNs
    {env}_server_settings.json              — instance types
    {env}_database_credentials.json         — RDS credentials (auto-generated)
    {env}_finalized_settings.json           — merged settings (auto-generated)
    {env}_remote_db_env.py                  — pushed to processing servers (auto-generated)

  deployment_helpers/              — Python modules used by launch_script.py
  pushed_files/                    — files SSH'd onto manager/worker EC2 instances
```

---

## CDK stack details

The `BeiwePrerequisitesStack` in `cdk/prerequisites_stack.py` creates:

**IAM user `beiwe-deploy`**
- Attached policy: `AdministratorAccess`
- Access key stored in Secrets Manager as `beiwe-deploy-credentials`
  ```json
  {"AWS_ACCESS_KEY_ID": "...", "AWS_SECRET_ACCESS_KEY": "..."}
  ```

**EC2 key pair `beiwe-deployment-key`**
- Private key stored by CloudFormation in SSM Parameter Store at `/ec2/keypair/{key-pair-id}` as a `SecureString`
- `setup_beiwe.py` retrieves it and saves it to `~/.ssh/beiwe-deployment-key.pem` with `chmod 600`

**Dedicated VPC (`BeiweVpc`)**
- 2 public subnets across 2 AZs, internet gateway, no NAT gateway
- Keeps Beiwe's resources isolated from anything else in the account
- VPC ID is output as `VpcId` and written into `global_configuration.json`
- **Known limitation:** processing servers are in public subnets because `launch_script.py` SSHes to their public IPs directly. Moving them to private subnets would require replacing that SSH approach with a bastion host or AWS Systems Manager Session Manager — a `launch_script.py` change outside the scope of this tooling. RDS is created with `PubliclyAccessible=False` and access-controlled by security groups regardless of subnet.

To deploy the stack directly (without `setup_beiwe.py`):
```
cd cluster_management/cdk/
cdk deploy --context region=us-east-2
```

To tear it down (this deletes the IAM user and key pair — only do this if you are decommissioning the deployment):
```
cdk destroy
```
