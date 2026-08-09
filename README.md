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
  Apply: Playwright via Browserbase (residential proxy, stealth)
         Adapters: Greenhouse, Lever, Ashby, Workday, iCIMS
        |
        v
  Approval queue (email w/ one-click approve) -> submit -> tracker
```

## Decisions log
- Cloud-first: GitHub Actions orchestration, Browserbase for browser sessions
- Approval-queue mode first, full-auto later once trusted
- Tailoring: reword/reorder existing content only
- LLM: Anthropic API
- Notifications: email (no dashboard)
- State: SQLite tracker DB committed as artifact / S3

## Layout
- `watcher/`  poll + diff listing repos, filtering rules
- `sprint.py` fast lane: every 4 min, brand-new postings are tailored + submitted immediately (speed-to-apply)
- `tailor/`   LaTeX resume tailoring + PDF compile
- `apply/`    per-ATS Playwright adapters
- `resume/`   your base LaTeX resume (source of truth)
- `profile/`  application answers (copy profile.example.yaml -> profile.yaml)
- `notify/`   email digests + approval links
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
