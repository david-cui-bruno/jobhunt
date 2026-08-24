# jobhunt

Autonomous job-application pipeline for David Cui. Goal: land a **winter or
summer 2027 SWE/ML internship** (or full-time at a YC/startup) by making
applications a background process: discover every relevant posting fast,
apply with a tailored resume within hours, surface only what genuinely needs
a human, and never misrepresent anything.

## What it does

```
DISCOVER                 FILTER                TAILOR               SUBMIT                TRACK               REPORT
GitHub listing repos --> role/season/       --> Claude rewords  --> resident local    --> Gmail classify --> ONE casual daily
YC Work at a Startup     location rules         LaTeX bullets       dispatcher polls       OA/interview/     digest, 6pm ET
HN hiring threads        canonical dedupe       (reword only,       every 30 seconds       recruiter/        Telegram + email;
A-D startup scout        no P26 batch           never fabricate)    by ATS lane            rejection/offer   replies are commands
```

- **Discovery** runs on launchd timers (macOS, this laptop). Sources: the big
  GitHub internship lists, YC's Work at a Startup (intern **and** full-time
  directories), HN hiring threads, and the **Series A-D startup scout**
  (`watcher/abc_startups.py`): harvests funding announcements + VC portfolio
  pages, LLM-extracts company/round (A-D, David 2026-08-19), then resolves
  each company's real careers page (official domain -> careers/jobs links ->
  ATS board URL; slug guessing only as a last resort) and polls the public
  Greenhouse/Lever/Ashby/Workable/SmartRecruiters JSON APIs on every cycle.
  `watcher/abc_backfill.py` seeded ~3 years of past rounds from the
  TechCrunch archive. A-D startups are the roles that never make the lists.
- **Filtering** enforces David's rules in code (`watcher/filter.py` +
  `profile/profile.yaml`): everything engineer-adjacent (SWE/ML/AI, MTS,
  founding/product/forward-deployed/solutions engineer, SRE/devops/security,
  embedded **software**, mobile, data science, quant, and PM/APM) with
  word-boundary matching; no hardware/EE/mechanical; winter + summer terms
  only (no fall/spring/co-op); full-time allowed for YC/startup sources;
  never P26-batch YC companies; US/remote; canonical posting dedupe before
  any external form or email is touched.
- **Tailoring**: each hourly drip invocation drains every currently claimable
  supported posting, one at a time with CAS claims. There is no per-run or
  daily tailoring cap. Claude rewords the base LaTeX resume against the JD
  using only the approved `resume/bullet_bank.md` (numbers pre-verified).
  Graduation date is **track-based** (David 2026-08-19, `track.py`): intern
  applications say May 2028, full-time applications say May 2027 (his real
  early-graduation plan). A structural quality gate blocks broken PDFs from
  ever reaching an ATS.
- **Submission** is a resident local dispatcher (`submit_daemon.py`) polling every
  30 seconds. It groups ready postings by the shared ATS lane classifier:
  direct lanes for Greenhouse, Lever, Workable, and Rippling run with bounded
  concurrency, Workday has its own single-worker lane, Ashby remains behind its
  breaker policy, and unsupported ATSs park as manual. Lane policy separates
  automatic submission from resume preparation: a `preparable-manual` row may
  receive a tailored resume and safe attempt artifact, but its preparation
  destination is `manual`, never hidden in `ready`. Existing nonautomatic ready
  rows are reconciled to manual with an exact handoff reason before rollout.
  Headless Playwright adapters handle Greenhouse, Lever, Ashby, Workday,
  Workable, SmartRecruiters, Rippling, plus WaaS founder messages and email
  applications. The
  **form Q&A engine** (`apply/qa.py`) answers questions
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
- **Dreamwork wrapper resolution** is preview-first. Dreamwork source URLs stay as
  provenance while the resolver stores the public ATS target, resolver name,
  source-page hash, and sanitized row-local error in `posting_url_resolutions`.
  Cached results are reused only when both `posting_id` and `source_url` match.
  Resolution and canonical checks do not perform network I/O inside identity,
  dedupe, or submission claim transactions. Resolver fetches do not follow
  redirects, and accepted targets must be strict public HTTP(S) DNS names or
  global IP literals. Numeric loopback/private spellings, private IPs,
  credentials, localhost, and internal-only hostnames fail closed. Canonical
  collisions are fail-closed: applied aliases, active queued, tailoring, ready,
  manual, submitting, or sprinting aliases, terminal aliases, and conservative
  same-company/title Dreamwork applied siblings are not rewritten or requeued.
  Failed wrapper resolutions are negative-cached with backoff, and each watcher
  run has count and wall-time budgets so dead wrappers cannot starve drip.
  Unsupported, unsafe, missing-link, click-uncertain, stale, skipped, submitted,
  finished-attempt, CAPTCHA, spam, and user-skipped rows remain manual or
  terminal until a human reviews them. Use an existing database or a SQLite
  backup copy for dry runs. The CLI refuses nonexistent or unmigrated databases
  with JSON errors and never creates a mistyped path:

  ```bash
  scratch="${TMPDIR:-/tmp}/jobhunt-resolution-acceptance.db"
  sqlite3 out/tracker.db ".backup '$scratch'"
  python3 scripts/retriage_resolved_postings.py --db "$scratch" --preview --json
  ```

  For live rollout, first create a SQLite `.backup`, set the backup mode to
  `0600`, run `PRAGMA integrity_check` on both files, inspect the preview counts
  and samples by ATS and conflict reason, then run a reviewed bounded apply. The
  default apply cap is 25 rows; choose a smaller canary with `--limit N` when
  appropriate:

  ```bash
  python3 scripts/retriage_resolved_postings.py --db out/tracker.db --preview --json
  python3 scripts/retriage_resolved_postings.py --db out/tracker.db --apply --json --limit 25
  ```

  Safe reruns are allowed because preview opens read-only and preserves the
  database byte hash, apply recomputes only the selected bounded IDs under
  `BEGIN IMMEDIATE`, and updates use posting ID, manual status, and original
  `last_error` compare-and-set guards. After apply, verify zero active canonical
  duplicate groups, zero active aliases of application ledger rows, zero
  offseason active rows, zero finished-attempt rows requeued, and that unresolved
  or unsafe rows remain manual. Live Dreamwork backup, preview, apply,
  resident-pipeline observation, and Sheet readback remain pending broad review
  and coordinator execution.
- **Workday recoverable-row requeue** is preview-first and limited to the two
  repaired Workday entry failure prefixes: `resume upload zone never appeared`
  and `apply button not found (posting closed?)`. Preview is the default and
  opens the database read-only, returning only safe fields: posting ID, company,
  title, tenant, reason, attempt count, and URL. It excludes application ledger
  rows, any finished click-attempted or confirmed attempt, stale or submitted
  rows, active or applied canonical aliases, non-Workday URLs, and unrelated
  failures. It supports legacy databases without `submission_attempts`, while
  retaining all attempt exclusions when the ledger exists:

  ```bash
  python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --preview --json
  ```

  Live steps remain pending coordinator approval. Before any live apply, create a
  SQLite backup with mode `0600`, verify integrity on both files, and inspect the
  preview JSON:

  ```bash
  backup="${TMPDIR:-/tmp}/tracker-workday-requeue-$(date +%Y%m%d%H%M%S).db"
  sqlite3 out/tracker.db ".backup '$backup'"
  chmod 0600 "$backup"
  sqlite3 out/tracker.db 'PRAGMA integrity_check;'
  sqlite3 "$backup" 'PRAGMA integrity_check;'
  python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --preview --json
  ```

  Apply uses `BEGIN IMMEDIATE`, recomputes eligibility inside the transaction,
  and compare-and-sets only requested rows that are still `failed` or `manual`.
  It preserves `attempt_count` and all ledgers, clears `outcome`, and writes
  `last_error='requeued after Workday entry repair'`. Use explicit posting IDs
  or a small bounded canary, then verify with a fresh preview and ledger checks:

  ```bash
  python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --apply --json --posting-id POSTING_ID
  python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --apply --json --limit 5
  python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --preview --json
  ```

  Rollback is file-level only before the resident dispatcher consumes requeued
  rows. Stop the dispatcher, copy the reviewed backup over `out/tracker.db`, run
  `PRAGMA integrity_check`, and restart only after inspection. Do not run live
  apply without coordinator approval.
- **Ashby canary** is disabled by default and controlled only by SQLite state in
  `out/tracker.db`, not by files such as legacy cooldown markers. Operators use
  `python3 manage_lanes.py status ashby`, `preview ashby`, `enable-canary ashby`,
  and `pause ashby` to inspect or change that policy. Preview prints candidate
  identity, role, canonical URL, prior completed-attempt count, and resume
  quality without claiming or executing. The policy has evidence-driven tiers of
  180, 90, and 45 minutes, advances after 3 and 10 confirmed submissions, uses a
  24-hour spam breaker plus one-tier rollback, pauses on uncertainty, never
  retries a click-uncertain posting, and never reuses any posting with a
  completed attempt. Live canary and service rollout remain deferred.
- **Ashby browser verification** requires a separately installed Chrome for
  Testing when ordinary Chrome is in use. The default local target must expose
  the exact executable and process name `Ashby Chrome for Testing` at
  `/Applications/Ashby Chrome for Testing.app/Contents/MacOS/Ashby Chrome for Testing`.
  This separate name prevents the Ashby hide watchdog from touching the lister's
  `Google Chrome for Testing` process. Stable Google Chrome is never accepted
  for the Ashby about:blank smoke and ordinary Chrome must never be hidden.
  `python3 scripts/verify_ashby_browser.py --check-only [--json]` resolves the
  target without launching or invoking System Events. `--about-blank [--json]`
  is reserved for reviewed operator use after a dedicated target exists, visits
  only `about:blank`, uses the persistent local profile under the ignored
  `.jobhunt-browser-profiles/ashby` path, and reports only the executable,
  process name, profile path, user agent, `navigator.webdriver`, plugin count,
  and process-scoped visibility. The Ashby solution does not fabricate browser
  fingerprint fields and hiding is scoped only to the dedicated process.
- **Ashby live gate order** is exact: install a dedicated browser, run
  about:blank verification, integrate the reviewed branch, back up live
  `out/tracker.db` and run `PRAGMA integrity_check`, inspect the read-only live
  preview, obtain explicit user approval for one irreversible submission, run
  one canary cycle, immediately pause, and inspect telemetry. The dedicated
  browser install, two native about:blank smokes, branch integration, live DB
  backup and integrity check, schema migration, and disabled-lane preview have
  passed on the resident Mac. User approval, the live canary, telemetry review,
  and service reload remain pending.
- **Tracking** (every 30 min): reads Gmail, classifies replies (OA invite /
  interview / recruiter reply / rejection / offer), applies labels, archives
  noise, extracts deadlines.
- **Reporting**: exactly ONE email per day (6pm ET) plus a Telegram copy via
  kith-bridge. This one-digest rule is the only routine notification path: no
  per-posting Telegram, email, browser-popup, or Sheet-update notice is sent.
  The digest is casual, skimmable in 30 seconds, and separates work into
  verification before retrying, finish-manually actions, unanswered questions,
  pipeline stats, top failing ATSs, per-ATS confirmed ratios, lane queue depths,
  and Ashby breaker state when paused. Click-uncertain attempts come only from
  the attempt ledger and appear under verification with no retry action.
  CAPTCHA and missing-field handoffs appear under finish manually. Engineering
  debt is grouped for agents, not as a request to David. The Google Sheet has a
  `Manual Actions` tab with the complete current handoff list. It contains
  company, role, ATS, action, exact application URL, age, attempt state, latest
  reason, and booleans for prepared resume or local artifact availability, but
  never local paths, cookies, headers, credentials, or form answers. Replying
  "skip X" or "for <company>: <answer>" is executed by `digest_replies.py`;
  freeform replies land in the daily agent review. All other notification
  emails are muted (`notify/mailer.py` logs them to `out/notices.log`).

## Safety rails

- Append-only `applications` ledger; double-submits are structurally blocked.
- Append-only `submission_attempts` ledger records every external attempt with
  ATS, lane, worker, timestamps, outcome, confirmation, and artifact references.
- Canonical posting dedupe prevents mirrored postings from creating duplicate
  applications before any external form is touched.
- Screenshot of every filled form (14-day rotation) + an audit line for every
  auto-answered question (`out/qa_answers.log`).
- Never fabricates: a required field the profile can't truthfully answer
  stops that one application.
- Volume is not artificially capped. Lane concurrency, per-posting timeouts,
  canonical dedupe, and fail-closed uncertainty quarantine bound risk instead.

- CAPTCHA handling is preflight-only. Lever hCaptcha and SmartRecruiters
  DataDome markers stop before the submit marker, return a definitive manual
  outcome, set `click_attempted=false`, and set `submission_uncertain=false`.
  The pipeline never solves, bypasses, or retries a CAPTCHA.
- Local artifacts stay local. Filled-form screenshots and structured missing
  fields are stored in ignored Mac-only runtime paths and the attempt ledger.
  Digest and Sheet views may expose availability booleans and sanitized action
  text, but never artifact paths or raw answers.
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
3.9 - keep code 3.9-compatible):

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

- `watcher/`   poll + diff listing sources, filtering rules, A-D scout + backfill
- `sprint.py`  fast lane: every 4 min, brand-new postings are tailored +
               submitted immediately (speed-to-apply beats everything)
- `drip.py`    hourly orchestrator: discovery, tailor batch, email applies
- `submit_daemon.py` resident 30-second dispatcher for ready postings by ATS lane
- `submit.py`  legacy entrypoint and shared submit helpers
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
| com.jobhunt.submit | resident | 30-second ATS lane dispatcher |

## Launchd runtime secrets

The committed jobhunt launchd plists do not store long-lived provider secrets.
They invoke `runtime_secrets.py` first, which reads
`~/.config/jobhunt/runtime.env`, requires current-user ownership and mode `0600`,
loads only allowlisted secret keys, and then replaces itself with the target
Python process via `os.execvpe`.

One-time migration from already installed LaunchAgents is handled by:

```bash
python3 scripts/migrate_launchd_secrets.py \
  --launch-agents "$HOME/Library/LaunchAgents" \
  --output "$HOME/.config/jobhunt/runtime.env"
```

The migration writes the env file atomically with mode `0600` and prints key
names only. Review the generated file permissions before installing sanitized
plists or reloading launchd agents. Provider-side rotation is recommended for
any key that previously appeared in plists or Git history, but revoking and
issuing a replacement key requires separate approval.

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

1. A-D scout maturation: more funding sources, generic careers-page parsing
   for non-standard ATSes,
   compounding coverage of the startups that never hit the lists.
2. Extend best-judgment answering to identity-trivial required fields
   ("Legal Name", "Country") to shrink the manual pile further.
3. iCIMS (and other missing ATS) via adapter or email fallback.
4. Queue prioritization: target companies and quant/YC before FIFO.
