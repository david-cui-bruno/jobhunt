# jobhunt

Autonomous job-application pipeline for David Cui. Goal: land a **winter or
summer 2027 SWE/ML internship** (or full-time at a YC/startup) by making
applications a background process: discover every relevant posting fast,
apply with a tailored resume within hours, surface only what genuinely needs
a human, and never misrepresent anything.

## What it does

```
DISCOVER                 FILTER                TAILOR               SUBMIT                TRACK               REPORT
GitHub listing repos --> role/season/       --> Claude rewords  --> headless Playwright --> Gmail classify --> ONE casual daily
YC Work at a Startup     location rules         LaTeX bullets       per-ATS adapters        OA/interview/     digest, 6pm ET
HN hiring threads        1 app per company      (reword only,       8 per run, paced        recruiter/        email + iMessage;
A/B/C startup scout      no P26 batch           never fabricate)    every 65 min            rejection/offer   replies are commands
```

- **Discovery** runs on launchd timers (macOS, this laptop). Sources: the big
  GitHub internship lists, YC's Work at a Startup (intern **and** full-time
  directories), HN hiring threads, and the **Series A/B/C startup scout**
  (`watcher/abc_startups.py`): harvests funding announcements + VC portfolio
  pages, LLM-extracts company/round (A/B/C only), resolves each company's
  Greenhouse/Lever/Ashby board via their public JSON APIs, then polls those
  boards on every cycle. A-C startups are the roles that never make the lists.
- **Filtering** enforces David's rules in code (`watcher/filter.py` +
  `profile/profile.yaml`): SWE/ML titles; winter + summer terms only (no
  fall/spring/co-op); full-time allowed for YC/startup sources; never
  P26-batch YC companies; US/remote; one application per company ever.
- **Tailoring** (≤100/day): Claude rewords the base LaTeX resume against the
  JD using only the approved `resume/bullet_bank.md` (numbers pre-verified,
  grad date June 2028 immutable). A structural quality gate blocks broken
  PDFs from ever reaching an ATS.
- **Submission** (8 per run, runs every 65 min, 15-45s jittered pacing):
  headless Playwright adapters for Greenhouse, Lever, Ashby, Workday,
  Workable, SmartRecruiters, Rippling, plus WaaS founder messages and email
  applications. The **form Q&A engine** (`apply/qa.py`) answers questions
  from the profile + story bank under a two-tier policy:
  - HARD-blocked (never auto-answered, even required): demographics,
    compensation, references, work history claims, anything a wrong answer
    would misrepresent.
  - SOFT topics (clearance status, graduation timing, availability): answered
    with truthful best judgment **only when the field is required**; optional
    fields stay blank.
  - Essays draw only on `docs/interview_stories.md` (the story bank). The
    story bank never references this pipeline itself (a recruiter once
    clocked the application as machine-written from exactly that).
  Anything unanswerable parks as `manual` off to the side and never blocks
  the postings behind it.
- **Tracking** (every 30 min): reads Gmail, classifies replies (OA invite /
  interview / recruiter reply / rejection / offer), applies labels, archives
  noise, extracts deadlines.
- **Reporting**: exactly ONE email per day (6pm ET) plus an iMessage copy via
  kith-bridge, casual tone, skimmable in 30 seconds: what needs David
  (deadline-sorted), stuck applications with the exact blocking question,
  unverified submissions, pipeline stats. Replying "skip X" or
  "for <company>: <answer>" is executed by `digest_replies.py`; freeform
  replies land in the daily agent review. All other notification emails are
  muted (`notify/mailer.py` logs them to `out/notices.log`).

## Safety rails

- Append-only `applications` ledger; double-submits are structurally blocked.
- Screenshot of every filled form (14-day rotation) + an audit line for every
  auto-answered question (`out/qa_answers.log`).
- Never fabricates: a required field the profile can't truthfully answer
  stops that one application.
- Caps: 8 submissions/run, 100 tailors/day, one app per company forever.
- Gmail token failover: if jobhunt's OAuth token is revoked, `notify/mailer`
  falls back to kith's healthy token for the same account instead of going
  silent.
- Self-healing: the fleet doctor (`~/.fleet/fleet-doctor.sh`, every 30 min)
  health-checks the timers and DB freshness, auto-remediates, and escalates
  what it can't fix into the daily digest + a daily agent review session.

## Companion: kith recruiter outreach

The kith CRM (separate repo) runs the recruiter arm: harvested in-house
recruiters at target companies get paced no-note LinkedIn connects (≤8/day);
each accept generates David's fixed intro DM (Framewise Health YC line, a
role-matched project fact) which David approves per-message before it sends.
`sync_queue.py` mirrors the active queue into kith so the referral surface
stays current.

## Clone and verify

The canonical branch is `main` in the private GitHub repository. Python 3.12
is recommended for local and production use (the resident Mac currently runs
3.9 — keep code 3.9-compatible):

```bash
git clone https://github.com/david-cui-bruno/jobhunt.git
cd jobhunt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s . -p 'test*.py'
```

The clone contains the application code, tests, deployment manifests, resume
source, and candidate profile. It intentionally does **not** contain runtime
secrets, browser sessions, the SQLite tracker, generated PDFs, Terraform state,
or the decrypted application-answer bank. Those are restored separately for a
live deployment. Start from these checked-in templates when configuring a new
machine:

- `deploy/jobhunt.env.example`
- `profile/application_answers.example.yaml`
- `deploy/aws/backend.hcl.example`
- `deploy/aws/terraform.tfvars.example`

Run tests before configuring credentials or enabling any timers. The AWS and
VPS installers leave outbound timers disabled until an operator verifies the
environment, queue, browser session, and application-answer file.

## Layout

- `watcher/`   poll + diff listing sources, filtering rules, A/B/C scout
- `sprint.py`  fast lane: every 4 min, brand-new postings are tailored +
               submitted immediately (speed-to-apply beats everything)
- `drip.py`    hourly orchestrator: discovery, tailor batch, email applies
- `submit.py`  the submitter: ready postings -> ATS adapters (every 65 min)
- `tailor/`    LaTeX resume tailoring + PDF compile + quality gate
- `apply/`     per-ATS Playwright adapters + the form Q&A engine
- `inbox.py`   Gmail classification + the daily digest trigger
- `digest.py`  the one daily email/text; `digest_replies.py` executes replies
- `resume/`    base LaTeX resume + approved bullet bank (source of truth)
- `profile/`   rules + application answers (copy the examples)
- `docs/`      interview story bank (essay grounding)
- `notify/`    Gmail send/read helpers (+ kith token failover)
- `out/`       tracker.db, logs, screenshots, audit trails (never committed)

## Timers (launchd, this Mac)

| agent | cadence | job |
|---|---|---|
| com.jobhunt.sprint | 4 min | fast-lane new postings |
| com.jobhunt.revise | 5 min | legacy review-thread poller |
| com.jobhunt.queue-sync | 15 min | mirror queue into kith |
| com.jobhunt.inbox | 30 min | Gmail classify + digest + digest replies |
| com.jobhunt.drip | 60 min | discovery + tailor batch |
| com.jobhunt.submit | 65 min | submit up to 8 ready postings |

## Decisions log (abridged)

- Full-auto submission; per-item approval emails retired in favor of the
  daily digest (2026-08-17).
- Tailoring is reword/reorder only; fabrication is a bug, not a feature.
- Winter + summer terms only; no fall/spring/co-op; no P26 YC batch;
  full-time OK at YC/startups (2026-08-17).
- Essays: FULL AUTO from the story bank (ratified 2026-08-08); story bank
  must never describe this pipeline (2026-08-17).
- The gaming/fullscreen submit deferral was removed 2026-08-17: it matched
  the idle Steam client and silently froze the pipeline for weeks. Headless
  adapters never show a window, so the check protected nothing.
- State: local SQLite (`out/tracker.db`); macOS launchd is the scheduler.
  The VPS/AWS manifests below are the portable deployment path.

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

## Roadmap

1. A/B/C scout maturation: more funding sources, better slug resolution,
   compounding coverage of the startups that never hit the lists.
2. Extend best-judgment answering to identity-trivial required fields
   ("Legal Name", "Country") to shrink the manual pile further.
3. iCIMS (and other missing ATS) via adapter or email fallback.
4. Queue prioritization: target companies and quant/YC before FIFO.
