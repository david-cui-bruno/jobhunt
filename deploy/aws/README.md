# AWS EC2 deployment

This directory turns the generic Ubuntu VPS package into a reviewable AWS
configuration. It deliberately uses **EC2 + EBS + systemd**, not ECS or
Fargate. The application has local mutable SQLite, OAuth, browser-session, and
resume state; a warm single host is the better fit until that state is moved to
PostgreSQL/S3 and every worker is made idempotent.

Nothing in this directory creates resources until `terraform apply` is run.
The application timers remain disabled after both infrastructure creation and
bootstrap.

## What Terraform creates

- Ubuntu 24.04 EC2, default `t3a.medium` (2 vCPU / 4 GiB, x86-64)
- Encrypted 40 GiB gp3 root volume
- Elastic IP for stable outbound identity
- Dedicated VPC/public subnet with **no inbound security-group rules**
- Systems Manager instance role for shell access without SSH
- CloudWatch Agent for memory, disk, syslog, and cloud-init telemetry
- Status-check recovery, CPU, memory, and CPU-credit alarms
- Optional SNS email alerts and an always-created $50 whole-account AWS Budget
- Private versioned S3 bucket for bootstrap transfer and application backups
- Daily AWS Backup recovery points with 14-day retention
- IMDSv2 requirement and a 2 GiB swap file

Expected steady-state cost is approximately $36-$40/month before unusual data
transfer or snapshot growth. Do not add a NAT Gateway; it would add roughly
$33/month before processing charges and is unnecessary for one public-subnet
instance with no inbound rules.

## Prerequisites

Install locally:

- Terraform 1.7+
- AWS CLI v2
- Session Manager plugin

Use AWS IAM Identity Center/SSO where possible:

```bash
aws configure sso
aws sts get-caller-identity
```

Do not use the AWS root user for normal operation, and do not store access keys
inside this repository.

## 1. Review and plan without creating resources

```bash
cd deploy/aws
cp terraform.tfvars.example terraform.tfvars
# Add alert_email if budget/alarm email notifications are desired.
# Before the first apply, the state bucket does not exist yet.
terraform init -backend=false
terraform fmt -check
terraform validate
terraform plan -out=jobhunt.tfplan
terraform show jobhunt.tfplan
```

Review the complete plan. The plan should show no inbound security-group rules,
one EC2 instance, one Elastic IP, one encrypted gp3 root volume, the private S3
bucket, backup resources, IAM roles, CloudWatch resources, and optional email
notifications.

## 2. Apply only after explicit approval

`terraform apply` creates billable AWS resources. It is intentionally a separate
step:

```bash
terraform apply jobhunt.tfplan
terraform output
```

Wait until the instance appears as an online managed node in Systems Manager.
Cloud-init installs monitoring and swap, but it does not copy application state
or enable workers.

### Move Terraform state off the laptop

The first apply necessarily uses local state because it creates the private
state bucket. Migrate immediately after that apply:

```bash
cp backend.hcl.example backend.hcl
# Replace AWS_ACCOUNT_ID in backend.hcl.
terraform init -migrate-state -force-copy -backend-config=backend.hcl
terraform state list
```

The S3 backend uses the bucket's versioning and encryption plus Terraform's
native S3 lock file. `backend.hcl` contains no secret, but is machine/account
specific and should not be committed.

## 3. Stop laptop workers and bootstrap

This is the production cutover point. First unload every local launchd worker.
Do not run laptop and EC2 workers against divergent SQLite databases.

```bash
for label in drip revise submit sprint inbox queue-sync; do
  launchctl bootout "gui/$(id -u)/com.jobhunt.${label}" 2>/dev/null || true
done
launchctl list | grep com.jobhunt || true
```

The final command must print nothing. Then run from `deploy/aws`:

```bash
CONFIRM_LOCAL_WORKERS_STOPPED=yes \
ANTHROPIC_API_KEY='your-key-in-this-shell-only' \
./stage-and-bootstrap.sh
```

The script:

1. Archives the committed application code.
2. Copies `out/` and non-log files from `secrets/` while writers are stopped.
3. Stores the Anthropic key as an SSM SecureString, not Terraform state or a
   process-list argument.
   Approved sensitive application answers use the same pattern at
   `/jobhunt/application_answers`; their value is never stored in Terraform or Git.
4. Uses the private S3 bucket as an encrypted bootstrap staging area and purges
   every staged object version on either success or failure.
5. Runs the existing Ubuntu installer through SSM.
6. Runs the repository test and submit dry-run.
7. Deletes the temporary bootstrap objects after success.
8. Leaves every timer disabled.

## 4. Reauthenticate Gmail

The current token is revoked. Start the callback tunnel:

```bash
./gmail-port-forward.sh
```

Keep it open. In another terminal, open a shell:

```bash
$(terraform output -raw ssm_shell_command)
```

Inside the instance:

```bash
sudo -u jobhunt env JOBHUNT_ROOT=/opt/jobhunt \
  /opt/jobhunt/.venv/bin/python /opt/jobhunt/deploy/reauth_gmail.py
```

Open the printed `AUTH_URL` on the laptop. The callback travels through SSM, so
no inbound port is opened.

## 5. Verify without sending or submitting

```bash
./verify-instance.sh
```

This checks SQLite, the repository test, systemd unit syntax, submit dry-run,
headless Chromium startup, LaTeX availability, and confirms timer state. It does
not run `drip.py`, `submit.py`, `sprint.py`, `inbox.py`, or a real queue sync.

## 6. Enable gradually

Open an SSM shell and enable one stage at a time:

```bash
# Watcher, autonomous tailoring, direct queueing, and inbox behavior.
sudo systemctl enable --now jobhunt@drip.timer jobhunt@inbox.timer

# Ordinary application submission only after reviewing the queue.
sudo systemctl enable --now jobhunt-submit.service

# Full-auto fast lane last.
sudo systemctl enable --now jobhunt@sprint.timer
```

Keep `jobhunt@revise.timer` disabled. Approval and revision-request emails are no
longer part of the production policy.

Enable Kith sync separately only if `/etc/jobhunt/kith.env` has been populated.

## Rollback

Stop EC2 workers without destroying state:

```bash
sudo systemctl disable --now \
  jobhunt@drip.timer jobhunt@revise.timer jobhunt@sprint.timer \
  jobhunt@inbox.timer jobhunt-queue-sync.timer jobhunt-submit.service
```

Do not run `terraform destroy` as an operational rollback. The EC2 instance and
state bucket have `prevent_destroy` guards. Restore the laptop database only
after confirming all EC2 timers are stopped.

## Later improvements

After the first stable week:

- Add application-consistent SQLite backup (`sqlite3 .backup` or Litestream) to
  the `backups/` S3 prefix. AWS Backup remains the coarse machine-level recovery
  layer.
- Add a dead-man heartbeat for every timer so a silent non-run alerts quickly.
- Consider Docker on EC2 for reproducible packaging.
- Consider Fargate only after replacing SQLite, externalizing files/session
  state, and implementing distributed locks and idempotent retries.
