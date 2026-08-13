#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT}/deploy/aws"
instance_id="$(terraform -chdir="${TF_DIR}" output -raw instance_id)"
region="$(terraform -chdir="${TF_DIR}" output -raw aws_region)"

exec aws ssm start-session --region "${region}" --target "${instance_id}"
