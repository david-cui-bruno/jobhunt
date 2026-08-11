#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./deploy/install-ubuntu.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${ROOT}" != "/opt/jobhunt" ]]; then
  echo "This installer expects the checkout at /opt/jobhunt; got ${ROOT}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates \
  python3-venv \
  python3-pip \
  build-essential \
  rsync \
  texlive-latex-base \
  texlive-latex-extra \
  poppler-utils

if ! id -u jobhunt >/dev/null 2>&1; then
  useradd --system --home-dir /opt/jobhunt --shell /usr/sbin/nologin jobhunt
fi

install -d -o jobhunt -g jobhunt -m 0750 /opt/jobhunt/out /opt/jobhunt/resume /opt/jobhunt/secrets /opt/jobhunt/.cache/ms-playwright
install -d -o root -g root -m 0750 /etc/jobhunt

if [[ ! -e /opt/jobhunt/.venv/bin/python ]]; then
  python3 -m venv /opt/jobhunt/.venv
fi
/opt/jobhunt/.venv/bin/python -m pip install --upgrade pip
/opt/jobhunt/.venv/bin/pip install --requirement /opt/jobhunt/requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/jobhunt/.cache/ms-playwright \
  /opt/jobhunt/.venv/bin/python -m playwright install --with-deps chromium

if [[ ! -e /etc/jobhunt/jobhunt.env ]]; then
  install -o root -g root -m 0600 /opt/jobhunt/deploy/jobhunt.env.example /etc/jobhunt/jobhunt.env
  echo "Created /etc/jobhunt/jobhunt.env. Fill in ANTHROPIC_API_KEY before enabling workers."
fi

for unit in /opt/jobhunt/deploy/systemd/*.service /opt/jobhunt/deploy/systemd/*.timer; do
  install -o root -g root -m 0644 "${unit}" /etc/systemd/system/"$(basename "${unit}")"
done

# The application must be able to update tracker state, generated resumes, and
# refreshed OAuth/session state, but source and deployment files remain root-owned.
chown -R jobhunt:jobhunt /opt/jobhunt/.venv /opt/jobhunt/out /opt/jobhunt/resume /opt/jobhunt/secrets /opt/jobhunt/.cache
chmod 0700 /opt/jobhunt/secrets
systemctl daemon-reload

echo
cat <<'EOF'
Installed the VPS runtime. Timers are intentionally NOT enabled.

Before enabling anything:
  1. Fill /etc/jobhunt/jobhunt.env (chmod 600).
  2. Copy secrets/gmail_client.json, gmail_token.json, and optionally yc_state.json.
  3. Run the dry-run checks in deploy/VPS.md.
  4. Enable only the timers you intend to run, one at a time.
EOF
