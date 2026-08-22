#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT}/deploy/aws"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

for command_name in aws terraform git tar python3 jq pgrep; do
  require_command "${command_name}"
done

if [[ "${CONFIRM_LOCAL_WORKERS_STOPPED:-}" != "yes" ]]; then
  cat >&2 <<'EOF'
Refusing to copy the production SQLite database while local workers may still
write to it. Stop and unload every com.jobhunt.* launchd agent, verify that
`launchctl list | grep com.jobhunt` prints nothing, then rerun with:

  CONFIRM_LOCAL_WORKERS_STOPPED=yes ANTHROPIC_API_KEY='...' ./stage-and-bootstrap.sh
EOF
  exit 1
fi

if launchctl list 2>/dev/null | grep -q 'com\.jobhunt\.'; then
  echo "Refusing cutover: one or more local com.jobhunt launchd agents are still loaded." >&2
  launchctl list | grep 'com\.jobhunt\.' >&2 || true
  exit 1
fi

worker_pattern='(^|[ /])(drip|revise|submit|sprint|inbox|sync_queue)\.py([[:space:]]|$)'
if pgrep -f "${worker_pattern}" >/dev/null 2>&1; then
  echo "Refusing cutover: one or more jobhunt Python workers are still running outside launchd." >&2
  pgrep -fl "${worker_pattern}" >&2 || true
  exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ANTHROPIC_API_KEY must be present in this shell. It is stored in SSM Parameter Store, not Terraform state." >&2
  exit 1
fi

instance_id="$(terraform -chdir="${TF_DIR}" output -raw instance_id)"
region="$(terraform -chdir="${TF_DIR}" output -raw aws_region)"
bucket="$(terraform -chdir="${TF_DIR}" output -raw state_bucket)"
parameter_name="$(terraform -chdir="${TF_DIR}" output -raw anthropic_parameter_name)"
kith_parameter_name="${parameter_name%/*}/kith_env"
application_answers_parameter_name="${parameter_name%/*}/application_answers"

workdir="$(mktemp -d)"
uploaded_prefix=""

purge_s3_prefix() {
  local prefix="$1"
  local versions_file="${workdir}/versions.json"
  local delete_file="${workdir}/delete-objects.json"

  aws s3api list-object-versions \
    --region "${region}" \
    --bucket "${bucket}" \
    --prefix "${prefix}/" \
    --output json >"${versions_file}"

  jq '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[] | {Key: .Key, VersionId: .VersionId}], Quiet: true}' \
    "${versions_file}" >"${delete_file}"

  if [[ "$(jq '.Objects | length' "${delete_file}")" -gt 0 ]]; then
    aws s3api delete-objects \
      --region "${region}" \
      --bucket "${bucket}" \
      --delete "file://${delete_file}" \
      >/dev/null
  fi
}

cleanup() {
  local exit_code=$?
  trap - EXIT
  set +e
  if [[ -n "${uploaded_prefix}" ]]; then
    purge_s3_prefix "${uploaded_prefix}" || echo "Warning: could not fully purge s3://${bucket}/${uploaded_prefix}/" >&2
  fi
  rm -rf "${workdir}"
  exit "${exit_code}"
}
trap cleanup EXIT

printf 'Creating immutable application archive from Git HEAD...\n'
git -C "${ROOT}" archive --format=tar.gz --output="${workdir}/app.tar.gz" HEAD

printf 'Creating an application-consistent state archive...\n'
mkdir -p "${workdir}/state"
tar \
  --exclude='*.log' \
  --exclude='*.lock' \
  -C "${ROOT}" \
  -cf - \
  out | tar -C "${workdir}/state" -xf -

python3 "${TF_DIR}/backup_sqlite.py" \
  "${ROOT}/out/tracker.db" \
  "${workdir}/state/out/tracker.db"

tar -C "${workdir}/state" -czf "${workdir}/state.tar.gz" out

tar \
  --exclude='*.log' \
  --exclude='*.pipe' \
  -C "${ROOT}" \
  -czf "${workdir}/secrets.tar.gz" \
  secrets

printf 'Writing Anthropic key to encrypted SSM Parameter Store...\n'
python3 - "${parameter_name}" "${workdir}/put-parameter.json" <<'PY'
import json
import os
import sys

payload = {
    "Name": sys.argv[1],
    "Type": "SecureString",
    "Value": os.environ["ANTHROPIC_API_KEY"],
    "Overwrite": True,
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
unset ANTHROPIC_API_KEY
aws ssm put-parameter \
  --region "${region}" \
  --cli-input-json "file://${workdir}/put-parameter.json" \
  >/dev/null
rm -f "${workdir}/put-parameter.json"

uploaded_prefix="bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-$(basename "${workdir}")"
for archive in app state secrets; do
  aws s3 cp \
    "${workdir}/${archive}.tar.gz" \
    "s3://${bucket}/${uploaded_prefix}/${archive}.tar.gz" \
    --region "${region}" \
    --sse AES256 \
    --only-show-errors
done

cat >"${workdir}/remote-bootstrap.sh" <<EOF
cloud-init status --wait
mkdir -p /opt/jobhunt /etc/jobhunt
aws s3 cp s3://${bucket}/${uploaded_prefix}/app.tar.gz /tmp/jobhunt-app.tar.gz --region ${region} --only-show-errors
aws s3 cp s3://${bucket}/${uploaded_prefix}/state.tar.gz /tmp/jobhunt-state.tar.gz --region ${region} --only-show-errors
aws s3 cp s3://${bucket}/${uploaded_prefix}/secrets.tar.gz /tmp/jobhunt-secrets.tar.gz --region ${region} --only-show-errors
tar -xzf /tmp/jobhunt-app.tar.gz -C /opt/jobhunt
tar -xzf /tmp/jobhunt-state.tar.gz -C /opt/jobhunt
tar -xzf /tmp/jobhunt-secrets.tar.gz -C /opt/jobhunt
chmod +x /opt/jobhunt/deploy/install-ubuntu.sh
/opt/jobhunt/deploy/install-ubuntu.sh
secret=\$(aws ssm get-parameter --region ${region} --name ${parameter_name} --with-decryption --query Parameter.Value --output text)
umask 077
printf 'ANTHROPIC_API_KEY=%s\nJOBHUNT_HEADLESS=1\nJOBHUNT_POSTING_TIMEOUT_SECONDS=300\nJOBHUNT_PLAYWRIGHT_TIMEOUT_MS=30000\nJOBHUNT_APPLICATION_ANSWERS_FILE=/etc/jobhunt/application_answers.yaml\n' "\$secret" > /etc/jobhunt/jobhunt.env
unset secret
chown root:root /etc/jobhunt/jobhunt.env
chmod 600 /etc/jobhunt/jobhunt.env
chown root:jobhunt /etc/jobhunt
chmod 750 /etc/jobhunt
if aws ssm get-parameter --region ${region} --name ${kith_parameter_name} --with-decryption --query Parameter.Value --output text > /etc/jobhunt/kith.env.tmp 2>/dev/null; then
  mv /etc/jobhunt/kith.env.tmp /etc/jobhunt/kith.env
  chown root:jobhunt /etc/jobhunt/kith.env
  chmod 640 /etc/jobhunt/kith.env
else
  rm -f /etc/jobhunt/kith.env.tmp
fi
if aws ssm get-parameter --region ${region} --name ${application_answers_parameter_name} --with-decryption --query Parameter.Value --output text > /etc/jobhunt/application_answers.yaml.tmp 2>/dev/null; then
  mv /etc/jobhunt/application_answers.yaml.tmp /etc/jobhunt/application_answers.yaml
  chown root:jobhunt /etc/jobhunt/application_answers.yaml
  chmod 640 /etc/jobhunt/application_answers.yaml
else
  rm -f /etc/jobhunt/application_answers.yaml.tmp
fi
cd /opt/jobhunt
systemctl disable --now jobhunt@drip.timer jobhunt@revise.timer jobhunt@sprint.timer jobhunt@inbox.timer jobhunt-queue-sync.timer jobhunt-submit.service || true
sudo -u jobhunt env PYTHONDONTWRITEBYTECODE=1 /opt/jobhunt/.venv/bin/python -m unittest discover -v -s /opt/jobhunt -p 'test*.py'
set -a
. /etc/jobhunt/jobhunt.env
set +a
sudo -u jobhunt --preserve-env=ANTHROPIC_API_KEY env PYTHONDONTWRITEBYTECODE=1 /opt/jobhunt/.venv/bin/python -c 'from sync_queue import read_active_postings; from submit import submit_ready; print("active_rows:", len(read_active_postings())); print("submit_dry_run:", submit_ready(limit=1, dry_run=True))'
unset ANTHROPIC_API_KEY
aws cloudwatch put-metric-data --region ${region} --namespace Jobhunt --metric-data MetricName=BootstrapSuccess,Value=1,Unit=Count
rm -f /tmp/jobhunt-app.tar.gz /tmp/jobhunt-state.tar.gz /tmp/jobhunt-secrets.tar.gz
echo BOOTSTRAP_OK
EOF

python3 - "${workdir}/remote-bootstrap.sh" "${workdir}/parameters.json" <<'PY'
import json
import shlex
import sys

script = open(sys.argv[1], encoding="utf-8").read()
command = "/bin/bash -euo pipefail -c " + shlex.quote(script)
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump({"commands": [command]}, handle)
PY

command_id="$(aws ssm send-command \
  --region "${region}" \
  --instance-ids "${instance_id}" \
  --document-name AWS-RunShellScript \
  --comment "Bootstrap jobhunt without enabling timers" \
  --timeout-seconds 7200 \
  --parameters "file://${workdir}/parameters.json" \
  --query 'Command.CommandId' \
  --output text)"

printf 'Waiting for SSM command %s...\n' "${command_id}"
status=""
for _ in $(seq 1 720); do
  status="$(aws ssm get-command-invocation \
    --region "${region}" \
    --command-id "${command_id}" \
    --instance-id "${instance_id}" \
    --query Status \
    --output text 2>/dev/null || true)"
  case "${status}" in
    Success|Failed|Cancelled|TimedOut|Cancelling)
      break
      ;;
  esac
  sleep 5
done

if [[ "${status}" != "Success" ]]; then
  case "${status}" in
    Failed|Cancelled|TimedOut|Cancelling) ;;
    *)
      aws ssm cancel-command \
        --region "${region}" \
        --command-id "${command_id}" \
        --instance-ids "${instance_id}" \
        >/dev/null 2>&1 || true
      ;;
  esac
  aws ssm get-command-invocation \
    --region "${region}" \
    --command-id "${command_id}" \
    --instance-id "${instance_id}" || true
  echo "Bootstrap did not finish successfully: ${status:-unknown}" >&2
  exit 1
fi

aws ssm get-command-invocation \
  --region "${region}" \
  --command-id "${command_id}" \
  --instance-id "${instance_id}" \
  --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'

purge_s3_prefix "${uploaded_prefix}"
uploaded_prefix=""

cat <<EOF
Bootstrap finished. No jobhunt timers were enabled.

Next:
  1. Reauthenticate Gmail using ./gmail-port-forward.sh and deploy/reauth_gmail.py.
  2. Run ./verify-instance.sh.
  3. Enable revise first, then drip/inbox, the submit daemon, and sprint last.
EOF
