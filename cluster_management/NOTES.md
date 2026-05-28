# Deployment Notes

Running notes on architectural decisions, known limitations, and future improvements for the Beiwe cluster deployment tooling.

---

## Security findings

Summary of findings from a review of the deployment wiki, `launch_script.py`, the deployment helpers, and the EB/Apache configuration. The private-subnet issue for EC2 processing servers is documented separately below.

### Critical

**AdministratorAccess on the deployment IAM user**
The CDK stack (and the wiki) attach `AdministratorAccess` to the `beiwe-deploy` user. This grants full control of the AWS account. `beiwe_automation_policy.json` already exists and lists the actual services needed — it should be used instead. Even that policy needs scoping (see High section).

**Recommendation:** Attach `beiwe_automation_policy.json` rather than `AdministratorAccess`. See also the High finding about that policy's own over-scoping.

---

### High

**`beiwe_automation_policy.json`: `iam:*` and `s3:*` on `Resource: "*"`**
`iam:*` on all resources is privilege escalation by definition — the credential can create new admin users. `s3:*` on all resources means the deployment credential can read or delete participant data from any bucket in the account.

**Recommendation:** Scope each action to specific resource ARNs. At minimum: restrict `iam:*` to the specific role/profile names Beiwe creates; restrict `s3:*` to the Beiwe data bucket and EB bucket ARNs; replace `rds:*` with the specific actions used (`CreateDBInstance`, `DescribeDBInstances`, `DescribeDBEngineVersions`).

---

**`beiwe_server_aws_access.json`: `s3:*` plus unconstrained SSM and Batch permissions**
The server IAM policy grants `s3:*` including `DeleteObject` and `DeleteBucket` on a glob ARN (`arn:aws:s3:::bucket-name*`) that could match unintended buckets. `ssm:PutParameter` and `batch:SubmitJob` are both on `Resource: "*"`.

**Recommendation:** Replace `s3:*` with an explicit allowlist (`GetObject`, `PutObject`, `ListBucket`, and `DeleteObject` only if deletion is needed). Use exact bucket ARNs without trailing wildcards. Scope SSM and Batch permissions to specific parameter path prefixes and job queue/definition ARNs.

---

**Default admin password is publicly documented with no forced rotation**
`default_admin` / `abcABC123!@#` is printed by `setup_beiwe.py` and in the wiki. There is no mechanism to block access until the password is changed.

**Recommendation:** Generate a random first-login password during deployment and print it once. Ideally, force a password change before any research function is accessible.

---

**SSH port 22 open to `0.0.0.0/0` on processing servers**
`open_tcp_port` in `elastic_compute_cloud.py` defaults to `0.0.0.0/0`. Every manager and worker server is reachable from the entire internet on port 22.

**Recommendation:** Restrict SSH to a known operator CIDR. The better long-term fix is AWS Systems Manager Session Manager, which eliminates the need for port 22 to be open and produces an audit trail (see also the private-subnet note below).

---

**RabbitMQ password passed as a plaintext CLI argument**
`launch_script.py` runs `rabbitmqctl add_user beiwe <password>` via `sudo()`, which exposes the password in the process table and shell history.

**Recommendation:** Supply the password via stdin (`echo 'password' | rabbitmqctl change_password beiwe`) rather than as a positional argument.

---

**Unverified remote script piped to bash at deploy time**
`launch_script.py` runs `curl https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash` with no checksum verification. A supply-chain compromise of that repository would result in arbitrary code execution on every new processing server.

**Recommendation:** Pin the installer to a specific commit hash and verify a checksum before execution, or vendor pyenv in the repo.

---

### Medium

**RDS: no deletion protection, no MultiAZ, no audit log exports**
`create_db_instance` in `rds.py` sets `MultiAZ=False` and omits `DeletionProtection`. A single AZ failure takes down the database and the credential can delete it without a protection gate.

**Recommendation:** Set `MultiAZ=True` for production instances, `DeletionProtection=True`, and add `EnableCloudwatchLogsExports=['postgresql', 'upgrade']`.

---

**TLS 1.0/1.1 not disabled and `SSLSessionTickets` enabled in Apache**
`00_application.conf` uses `SSLProtocol All -SSLv2 -SSLv3`, which still allows TLS 1.0 and 1.1 (both deprecated by RFC 8996 and prohibited by NIST SP 800-52 Rev. 2). `SSLSessionTickets On` weakens forward secrecy.

**Recommendation:** Change to `SSLProtocol All -SSLv2 -SSLv3 -TLSv1 -TLSv1.1`. Set `SSLSessionTickets Off`. Add `SSLCompression off`.

---

**S3 public access block not explicitly set on new buckets**
`s3.py` creates buckets with `ACL='private'` but never calls `put_public_access_block`. A future policy or ACL change could inadvertently make participant data public.

**Recommendation:** Call `put_public_access_block` with all four flags set to `True` immediately after creating any bucket.

---

**EB S3 bucket not covered by the TLS-enforcement bucket policy**
`s3_encrypt_eb_bucket` applies encryption but not the `s3_require_tls` policy that the data bucket gets.

**Recommendation:** Apply `s3_require_tls` to the EB bucket as well.

---

**Credential JSON files written without restrictive file permissions**
`create_finalized_configuration`, `write_rds_credentials`, and `write_aws_credentials` all use `open(..., 'w')` with the default umask, which is often `0o644` (world-readable). The private key is correctly `chmod 600` in `setup_beiwe.py` but the credential JSON files are not.

**Recommendation:** Call `os.chmod(path, 0o600)` after writing any file containing credentials or secrets.

---

**RabbitMQ password file stored inside the web application directory**
`REMOTE_RABBIT_MQ_PASSWORD_FILE_PATH` is `beiwe-backend/manager_ip` — inside the project directory on the remote server. A path traversal or web server misconfiguration could expose it.

**Recommendation:** Store this file outside the project directory (e.g. `/home/ubuntu/beiwe_manager_ip` or `/etc/beiwe/`).

---

**Hardcoded password in `configure_local_postgres` and `ami_env_config.py`**
The single-server AMI build path creates a Postgres user with the literal password `'password'` and `ami_env_config.py` has `FLASK_SECRET_KEY = 'replace_with_random_string'`.

**Recommendation:** Generate credentials randomly here the same way `generate_valid_postgres_credentials()` does for RDS.

---

### Low

**No HTTP-to-HTTPS redirect at the infrastructure layer**
The port-80 VirtualHost in `00_application.conf` serves the application directly rather than redirecting to HTTPS. A participant connecting over HTTP receives unencrypted responses.

**Recommendation:** Add `Redirect permanent / https://%{HTTP_HOST}%{REQUEST_URI}` in the port-80 VirtualHost, or configure the load balancer to return 301 for all HTTP traffic.

---

**No `Content-Security-Policy` header**
`00_application.conf` sets `X-Frame-Options` and `X-Content-Type-Options` but no CSP, providing no browser-level protection against XSS.

**Recommendation:** Add a baseline `Content-Security-Policy` header and tighten based on actual asset sources.

---

**`IgnoreHealthCheck` flag has no automatic revert**
`fix_deploy` in `elastic_beanstalk.py` sets `IgnoreHealthCheck: true` on the EB environment with no mechanism to revert it. If an operator forgets, unhealthy instances serve traffic indefinitely.

**Recommendation:** Log a prominent warning after `fix_deploy` and consider an automatic revert call after the deploy window.

---

## Private subnets for processing servers

**Status:** not implemented — known limitation

Processing servers (manager + workers) are currently deployed into public subnets and are assigned public IP addresses. The reason is purely mechanical: `launch_script.py` SSHes to those servers using their public IP, retrieved in `elastic_compute_cloud.get_manager_public_ip()` and `get_worker_public_ips()`. If the servers had no public IP, the Fabric SSH connection would fail and deployment would break.

For a HIPAA deployment handling sensitive participant data, these servers ideally belong in private subnets with no direct internet exposure. Making that change requires replacing the direct-SSH approach in `launch_script.py` with something that works from a private address — the two practical options are:

- **AWS Systems Manager Session Manager** — no open port 22 required, access controlled by IAM, full audit trail. Probably the right long-term answer.
- **Bastion host** — a small EC2 instance in a public subnet that acts as an SSH jump host. Simpler to implement but adds a machine to maintain.

RDS is already in reasonable shape: it is created with `PubliclyAccessible=False` and access is gated by security groups, so the database endpoint is not reachable from the internet regardless of subnet placement.
