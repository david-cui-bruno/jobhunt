# jobhunt

Automated job application pipeline for Summer 2027 internships.

## Architecture

```
GitHub repos (Summer2027 listings)
        |  poll via GitHub Actions cron (~15 min)
        v
  Watcher: diff new postings -> filter (role/location/auth rules)
        |
        v
  Tailor: Claude rewords LaTeX resume bullets against job description
          (reword-only, never fabricate) -> compile PDF
        |
        v
  Apply: Playwright via local Chromium (headless on VPS)
         Adapters: Greenhouse, Lever, Ashby, Workday, Rippling, SmartRecruiters
        |
        v
  Autonomous submit -> tracker -> email summaries/exceptions
```

## Decisions log
- Original cloud-first design: GitHub Actions orchestration, Browserbase for browser sessions
- Full-auto application flow; approval-request emails disabled
- Tailoring: reword/reorder existing content only
- LLM: Anthropic API
- Notifications: email (no dashboard)
- State: SQLite tracker DB committed as artifact / S3

The original cloud-first design is not the current runtime. The checked-in code
uses local Chromium, a local SQLite database, and macOS `launchd` today. The VPS
runbook below replaces `launchd` with `systemd` while keeping the same local
browser and SQLite model. Browserbase and GitHub Actions are not wired into this
checkout.

## Layout
- `watcher/`  poll + diff listing repos, filtering rules
- `sprint.py` fast lane: every 4 min, brand-new postings are tailored + submitted immediately (speed-to-apply)
- `tailor/`   LaTeX resume tailoring + PDF compile
- `apply/`    per-ATS Playwright adapters
- `resume/`   your base LaTeX resume (source of truth)
- `profile/`  application answers (copy profile.example.yaml -> profile.yaml)
- `notify/`   email summaries and actionable exception alerts

## VPS deployment

The current checkout can run continuously on an Ubuntu VPS with local
Playwright Chromium, SQLite, and systemd timers. See [`deploy/VPS.md`](deploy/VPS.md)
for provisioning, Gmail reauthentication, staged enablement, monitoring, and
rollback. Timers are intentionally installed disabled so the migration cannot
send or submit anything before the operator verifies the queue and credentials.

The selected AWS path is documented in [`deploy/aws/README.md`](deploy/aws/README.md).
It provisions EC2, encrypted EBS, a stable Elastic IP, SSM-only administration,
CloudWatch monitoring, budget alerts, private S3 staging, and daily AWS Backup
recovery points through Terraform. No AWS resource is created until an explicit
`terraform apply`.

## Kith referral queue sync

`sync_queue.py` mirrors only the current `queued`, `ready`, and `failed` postings
from `out/tracker.db` into Kith's owner-scoped `job_queue_snapshot` table. It
reads SQLite in read-only mode, refuses to publish an empty queue by default,
and removes older snapshot versions so stale or filtered-out jobs do not remain
visible in the deployed referral queue.

Run a one-time sync from this checkout with:

```bash
python3 sync_queue.py --env-file /path/to/kith/.env.local
```

The env file must provide `NEXT_PUBLIC_SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, and `OWNER_ID`. Install
`launchd/com.jobhunt.queue-sync.plist` in `~/Library/LaunchAgents` if the
deployed Kith referral queue should refresh automatically every 15 minutes.
