#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT}/deploy/aws"
instance_id="$(terraform -chdir="${TF_DIR}" output -raw instance_id)"
region="$(terraform -chdir="${TF_DIR}" output -raw aws_region)"

cat <<'EOF'
Keep this session open. In another terminal, open an SSM shell and run:

  sudo -u jobhunt env JOBHUNT_ROOT=/opt/jobhunt \
    /opt/jobhunt/.venv/bin/python /opt/jobhunt/deploy/reauth_gmail.py

Then open the printed AUTH_URL in the laptop browser.
EOF

exec aws ssm start-session \
  --region "${region}" \
  --target "${instance_id}" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8765"],"localPortNumber":["8765"]}'
