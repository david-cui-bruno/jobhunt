#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT}/deploy/aws"

for command_name in aws terraform python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done

instance_id="$(terraform -chdir="${TF_DIR}" output -raw instance_id)"
region="$(terraform -chdir="${TF_DIR}" output -raw aws_region)"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

cat >"${workdir}/remote-verify.sh" <<'EOF'
test -f /opt/jobhunt/out/tracker.db
test -f /opt/jobhunt/secrets/gmail_client.json
test -x /opt/jobhunt/.venv/bin/python
cd /opt/jobhunt
systemd-analyze verify /etc/systemd/system/jobhunt@.service /etc/systemd/system/jobhunt-submit.service /etc/systemd/system/jobhunt-queue-sync.service /etc/systemd/system/jobhunt@drip.timer /etc/systemd/system/jobhunt@revise.timer /etc/systemd/system/jobhunt@sprint.timer /etc/systemd/system/jobhunt@inbox.timer /etc/systemd/system/jobhunt-queue-sync.timer
sudo -u jobhunt env PYTHONDONTWRITEBYTECODE=1 /opt/jobhunt/.venv/bin/python -m unittest discover -v -s /opt/jobhunt -p 'test*.py'
set -a
. /etc/jobhunt/jobhunt.env
set +a
sudo -u jobhunt --preserve-env=ANTHROPIC_API_KEY env PYTHONDONTWRITEBYTECODE=1 /opt/jobhunt/.venv/bin/python -c 'from sync_queue import read_active_postings; from submit import submit_ready; print("active_rows:", len(read_active_postings())); print("submit_dry_run:", submit_ready(limit=1, dry_run=True))'
unset ANTHROPIC_API_KEY
sudo -u jobhunt env PLAYWRIGHT_BROWSERS_PATH=/opt/jobhunt/.cache/ms-playwright JOBHUNT_HEADLESS=1 /opt/jobhunt/.venv/bin/python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); page=b.new_page(); page.set_content("<title>jobhunt smoke</title><p>ok</p>"); assert page.title()=="jobhunt smoke"; b.close(); p.stop(); print("chromium_smoke: ok")'
pdflatex --version | head -1
for timer in jobhunt@drip.timer jobhunt@revise.timer jobhunt@sprint.timer jobhunt@inbox.timer jobhunt-queue-sync.timer; do
  test "$(systemctl is-enabled "${timer}" 2>/dev/null || true)" = "disabled"
done
test "$(systemctl is-enabled jobhunt-submit.service 2>/dev/null || true)" = "disabled"
echo VERIFY_OK
EOF

python3 - "${workdir}/remote-verify.sh" "${workdir}/parameters.json" <<'PY'
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
  --comment "Safe jobhunt verification" \
  --timeout-seconds 1800 \
  --parameters "file://${workdir}/parameters.json" \
  --query 'Command.CommandId' \
  --output text)"

status=""
for _ in $(seq 1 360); do
  status="$(aws ssm get-command-invocation \
    --region "${region}" \
    --command-id "${command_id}" \
    --instance-id "${instance_id}" \
    --query Status \
    --output text 2>/dev/null || true)"
  case "${status}" in
    Success|Failed|Cancelled|TimedOut|Cancelling) break ;;
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
fi

aws ssm get-command-invocation \
  --region "${region}" \
  --command-id "${command_id}" \
  --instance-id "${instance_id}" \
  --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}' || true

if [[ "${status}" != "Success" ]]; then
  echo "Verification did not finish successfully: ${status:-unknown}" >&2
  exit 1
fi
