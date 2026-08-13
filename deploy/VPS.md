# VPS deployment

This is the least-change deployment for the current jobhunt codebase. It uses an
Ubuntu VPS, a Python virtualenv, local Playwright Chromium, SQLite under
`out/`, and `systemd` timers. No proxy is required. Keep one stable egress
location and one persistent browser state per account.

## Recommended server

Start with Ubuntu 24.04, 2 vCPU, 4 GB RAM, and at least 40 GB disk. A 2 GB
instance may work for light queue traffic, but Chromium plus LaTeX is more
comfortable with 4 GB. Do not expose a public web port. Use SSH keys and a
host firewall that allows SSH only from your administration network if possible.

## 1. Copy the checkout

On the VPS, install Git and clone this repository at the path expected by the
units:

```bash
sudo apt-get update
sudo apt-get install -y git
sudo git clone <your-private-repository-url> /opt/jobhunt
cd /opt/jobhunt
```

The runtime state is intentionally not in Git. Before starting workers, copy
these files from the laptop while the local launchd jobs are stopped:

```bash
# Run from the laptop, replacing VPS_HOST.
rsync -a --exclude '*.log' /Users/davidcui824/jobhunt/out/ VPS_HOST:/tmp/jobhunt-out/
rsync -a /Users/davidcui824/jobhunt/secrets/ VPS_HOST:/tmp/jobhunt-secrets/

# Run on the VPS.
sudo rsync -a /tmp/jobhunt-out/ /opt/jobhunt/out/
sudo rsync -a /tmp/jobhunt-secrets/ /opt/jobhunt/secrets/
sudo chown -R jobhunt:jobhunt /opt/jobhunt/out /opt/jobhunt/secrets
sudo chmod 0700 /opt/jobhunt/secrets
```

Do not use `--delete` during the first migration. Keep the laptop copy as the
rollback point until the VPS passes smoke checks.

## 2. Install the runtime

```bash
cd /opt/jobhunt
sudo ./deploy/install-ubuntu.sh
sudoedit /etc/jobhunt/jobhunt.env
sudo chmod 600 /etc/jobhunt/jobhunt.env
```

`install-ubuntu.sh` installs Python dependencies, TeX, Poppler, and the
Playwright Chromium browser. It installs units but deliberately does not enable
or start timers. This prevents an unverified migration from submitting jobs or
sending mail.

## 3. Reauthenticate Gmail

The existing token is revoked. Copy `secrets/gmail_client.json` to the VPS,
then create an SSH tunnel from the laptop:

```bash
ssh -L 8765:127.0.0.1:8765 VPS_USER@VPS_HOST
```

In a second VPS shell, run:

```bash
sudo -u jobhunt env JOBHUNT_ROOT=/opt/jobhunt \
  /opt/jobhunt/.venv/bin/python /opt/jobhunt/deploy/reauth_gmail.py
```

The command prints an `AUTH_URL`. Open that URL in the laptop browser. Google
will return to the forwarded port, and the token will be written with mode
0600. Never put the token or client secret in Git, an env file, or a unit file.

## 4. Smoke checks before enabling workers

Run checks that cannot submit an application or send mail:

```bash
cd /opt/jobhunt
sudo -u jobhunt env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
from pathlib import Path
for path in Path('.').rglob('*.py'):
    if any(part in {'secrets', 'out', '__pycache__'} for part in path.parts):
        continue
    compile(path.read_text(), str(path), 'exec')
print('python_compile: ok')
PY
sudo -u jobhunt env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -v -s . -p 'test*.py'
sudo -u jobhunt .venv/bin/python - <<'PY'
from submit import submit_ready
from sync_queue import read_active_postings
print('active_rows:', len(read_active_postings()))
print('submit_dry_run:', submit_ready(limit=1, dry_run=True))
PY
sudo systemd-analyze verify /etc/systemd/system/jobhunt@.service \
  /etc/systemd/system/jobhunt-queue-sync.service \
  /etc/systemd/system/jobhunt@drip.timer \
  /etc/systemd/system/jobhunt@revise.timer \
  /etc/systemd/system/jobhunt@submit.timer \
  /etc/systemd/system/jobhunt@sprint.timer \
  /etc/systemd/system/jobhunt@inbox.timer \
  /etc/systemd/system/jobhunt-queue-sync.timer
```

Do not run `drip.py`, `sprint.py`, `submit.py`, `inbox.py`, or the real queue
sync as a smoke test. They can send mail, submit applications, label Gmail, or
mutate the remote queue.

## 5. Enable in stages

Start with the least consequential worker and inspect its journal. Each command
below is explicit so the full-auto sprint lane is not enabled accidentally.

After confirming Gmail reads work, enable the autonomous drip and inbox timers.
Tailored resumes enter the submission queue directly; approval-request emails
and the revision poller are disabled:

```bash
sudo systemctl enable --now jobhunt@drip.timer jobhunt@inbox.timer
```

Only after manually reviewing the queue should you enable application
submission:

```bash
sudo systemctl enable --now jobhunt@submit.timer
```

Leave `jobhunt@sprint.timer` disabled initially. It submits newly discovered
postings immediately. Enable it only after the ordinary drip and submit paths
are stable:

```bash
sudo systemctl enable --now jobhunt@sprint.timer
```

If Kith queue sync is needed, create `/etc/jobhunt/kith.env` from
`deploy/kith.env.example`, chmod it 600, and then enable:

```bash
sudo install -o root -g jobhunt -m 0640 deploy/kith.env.example /etc/jobhunt/kith.env
sudoedit /etc/jobhunt/kith.env
sudo systemctl enable --now jobhunt-queue-sync.timer
```

Inspect timers and logs with:

```bash
systemctl list-timers 'jobhunt*'
journalctl -u 'jobhunt@*.service' -f
```

## Rollback

To stop VPS activity without deleting state:

```bash
sudo systemctl disable --now \
  jobhunt@drip.timer jobhunt@revise.timer jobhunt@submit.timer \
  jobhunt@sprint.timer jobhunt@inbox.timer jobhunt-queue-sync.timer
```

The SQLite database and generated resumes remain under `/opt/jobhunt/out` and
`/opt/jobhunt/resume`. Keep the laptop copy untouched until the migration is
trusted. If browser or OAuth state becomes inconsistent, stop timers first,
restore only the affected file from the backup, and reauthorize Gmail rather
than copying a stale token over a working one.

## Known limitations

- This uses direct local Chromium on a datacenter IP. A VPS may receive more
  CAPTCHA or login challenges than the home connection. Do not rotate proxies or
  use them to bypass a checkpoint.
- Work at a Startup currently launches Chromium non-headlessly by default on
  macOS. Set `JOBHUNT_HEADLESS=1` on the VPS. The code now honors that flag.
- Gmail OAuth remains an interactive setup step. The token is a file secret,
  not an environment variable.
- SQLite is single-host state. Do not run the laptop and VPS workers at the
  same time against separate copies of the database.
- Approval-request emails are disabled. Daily/weekly summaries and actionable
  exception notifications remain enabled.
