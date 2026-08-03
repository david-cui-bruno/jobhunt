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
- `tailor/`   LaTeX resume tailoring + PDF compile
- `apply/`    per-ATS Playwright adapters
- `resume/`   your base LaTeX resume (source of truth)
- `profile/`  application answers (copy profile.example.yaml -> profile.yaml)
- `notify/`   email digests + approval links
