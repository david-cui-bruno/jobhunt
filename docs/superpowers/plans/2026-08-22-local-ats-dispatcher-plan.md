# Local ATS Dispatcher and Attempt Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the serial global submit loop with an always-on, ATS-partitioned local dispatcher that records every attempt, allows distinct roles at one company, and isolates blocked lanes.

**Architecture:** SQLite remains authoritative and gains append-only attempt telemetry plus canonical posting identity. A long-running dispatcher claims rows atomically, routes them into independent ATS lanes, and launches the existing killable adapter subprocesses with bounded concurrency. `submit.py` remains the single-posting execution facade while new focused modules own identity, lane policy, telemetry, and daemon orchestration.

**Tech Stack:** Python 3.9, SQLite WAL, `concurrent.futures`, launchd, systemd, existing Playwright adapter subprocesses, `unittest` and `pytest`

**Spec:** `docs/superpowers/specs/2026-08-22-hybrid-pipeline-ashby-recovery-design.md`

## Global Constraints

- The Mac remains the only final browser submission endpoint and the only writer to the authoritative SQLite database.
- Cloud queues are not introduced in this plan.
- A blocked ATS lane must not pause another ATS lane.
- Every external attempt must be represented in `submission_attempts`.
- An unconfirmed submit click is quarantined and is never retried automatically.
- Distinct canonical job IDs at one company may submit; the same canonical posting may not submit twice.
- SmartRecruiters, iCIMS, and unknown ATS rows must not enter automatic submit lanes.
- Final browser actions remain invisible and use the existing isolated worker timeout.
- Do not stage or commit `out/tracker.db`.

---

### Task 1: Centralize SQLite runtime configuration and add the attempt ledger

**Files:**
- Create: `submission/__init__.py`
- Create: `submission/database.py`
- Create: `submission/attempts.py`
- Create: `tests/test_submission_attempts.py`
- Modify: `submit.py:21-25,124-160`
- Modify: `drip.py:16-20,179-183`

**Interfaces:**
- Produces: `connect_tracker(path: Path = DB) -> sqlite3.Connection`
- Produces: `ensure_submission_attempts(conn: sqlite3.Connection) -> None`
- Produces: `start_attempt(conn, *, attempt_id: str, posting_id: str, ats: str, lane: str, worker_id: str, browser_mode: str, policy_revision: str, started_at: int | None = None) -> None`
- Produces: `finish_attempt(conn, *, attempt_id: str, outcome: str, reason_code: str, raw_reason: str, click_attempted: bool, confirmation_observed: bool, artifact_refs: dict[str, str] | None = None, finished_at: int | None = None) -> None`
- Consumes: existing `out/tracker.db` schema and `postings.posting_id`

- [ ] **Step 1: Write the failing database tests**

```python
# tests/test_submission_attempts.py
import sqlite3
from pathlib import Path

import pytest

from submission.attempts import ensure_submission_attempts, finish_attempt, start_attempt
from submission.database import connect_tracker


def test_connect_tracker_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    conn.close()


def test_attempt_ledger_is_append_only_and_finishes_once() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_submission_attempts(conn)
    start_attempt(
        conn,
        attempt_id="a1",
        posting_id="p1",
        ats="greenhouse",
        lane="direct",
        worker_id="worker-1",
        browser_mode="isolated-headless",
        policy_revision="dispatcher-v1",
        started_at=100,
    )
    finish_attempt(
        conn,
        attempt_id="a1",
        outcome="submitted",
        reason_code="confirmed",
        raw_reason="confirmed",
        click_attempted=True,
        confirmation_observed=True,
        artifact_refs={"screenshot": "out/screenshots/p1.png"},
        finished_at=110,
    )
    row = conn.execute(
        "SELECT posting_id,ats,lane,outcome,reason_code,click_attempted,confirmation_observed "
        "FROM submission_attempts WHERE attempt_id='a1'"
    ).fetchone()
    assert row == ("p1", "greenhouse", "direct", "submitted", "confirmed", 1, 1)
    with pytest.raises(sqlite3.IntegrityError):
        start_attempt(conn, attempt_id="a1", posting_id="p1", ats="greenhouse", lane="direct",
                      worker_id="worker-2", browser_mode="isolated-headless",
                      policy_revision="dispatcher-v1", started_at=120)
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure**

Run: `python3 -m pytest -q tests/test_submission_attempts.py`

Expected: FAIL because `submission.database` and `submission.attempts` do not exist.

- [ ] **Step 3: Implement the connection helper and schema**

```python
# submission/database.py
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "tracker.db"


def connect_tracker(path: Path = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

```python
# submission/attempts.py
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_attempts (
    attempt_id TEXT PRIMARY KEY,
    posting_id TEXT NOT NULL,
    ats TEXT NOT NULL,
    lane TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    browser_mode TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    duration_ms INTEGER,
    outcome TEXT,
    reason_code TEXT,
    raw_reason TEXT,
    click_attempted INTEGER NOT NULL DEFAULT 0,
    confirmation_observed INTEGER NOT NULL DEFAULT 0,
    artifact_refs_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS submission_attempts_posting_idx
ON submission_attempts(posting_id, started_at);
CREATE INDEX IF NOT EXISTS submission_attempts_ats_idx
ON submission_attempts(ats, started_at);
"""


def ensure_submission_attempts(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def start_attempt(conn, *, attempt_id, posting_id, ats, lane, worker_id,
                  browser_mode, policy_revision, started_at=None) -> None:
    conn.execute(
        "INSERT INTO submission_attempts "
        "(attempt_id,posting_id,ats,lane,worker_id,browser_mode,policy_revision,started_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (attempt_id, posting_id, ats, lane, worker_id, browser_mode,
         policy_revision, started_at or int(time.time())),
    )
    conn.commit()


def finish_attempt(conn, *, attempt_id, outcome, reason_code, raw_reason,
                   click_attempted, confirmation_observed, artifact_refs=None,
                   finished_at=None) -> None:
    end = finished_at or int(time.time())
    changed = conn.execute(
        "UPDATE submission_attempts SET finished_at=?, "
        "duration_ms=(?-started_at)*1000, outcome=?, reason_code=?, raw_reason=?, "
        "click_attempted=?, confirmation_observed=?, artifact_refs_json=? "
        "WHERE attempt_id=? AND finished_at IS NULL",
        (end, end, outcome, reason_code, raw_reason[:1000], int(click_attempted),
         int(confirmation_observed), json.dumps(artifact_refs or {}, sort_keys=True), attempt_id),
    ).rowcount
    if changed != 1:
        conn.rollback()
        raise sqlite3.IntegrityError(f"attempt already finished or missing: {attempt_id}")
    conn.commit()
```

- [ ] **Step 4: Route `submit.py` and `drip.py` connections through `connect_tracker`**

Replace direct `sqlite3.connect(DB)` calls used by runtime workers with `connect_tracker(DB)`. Keep in-memory test connections unchanged. Call `ensure_submission_attempts(conn)` from `_ensure_outcome_columns` so live databases migrate idempotently.

- [ ] **Step 5: Run focused and regression tests**

Run: `python3 -m pytest -q tests/test_submission_attempts.py test_submit.py test_throughput.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add submission/__init__.py submission/database.py submission/attempts.py \
  tests/test_submission_attempts.py submit.py drip.py
git commit -m "Add submission attempt ledger"
```

### Task 2: Replace company-wide suppression with canonical posting identity

**Files:**
- Create: `submission/identity.py`
- Create: `tests/test_submission_identity.py`
- Modify: `submit.py:274-340,381-389,406-459`
- Modify: `sprint.py:110-129`
- Modify: `test_submit.py:372-470,568-596`

**Interfaces:**
- Produces: `canonical_posting_key(posting_id: str, url: str) -> str`
- Produces: `posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool`
- Produces: `claim_submission(conn, *, posting_id: str, url: str, from_status: str = "ready") -> str`
- Consumes: `apply.jd.canonical_application_url`

- [ ] **Step 1: Replace the old expectation with failing identity tests**

```python
# tests/test_submission_identity.py
import sqlite3

from submission.identity import canonical_posting_key, posting_already_applied


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE postings (posting_id TEXT PRIMARY KEY, company TEXT, url TEXT, status TEXT)")
    conn.execute("CREATE TABLE applications (posting_id TEXT PRIMARY KEY)")
    return conn


def test_distinct_roles_at_one_company_are_not_duplicates() -> None:
    conn = db()
    conn.executemany("INSERT INTO postings VALUES (?,?,?,?)", [
        ("p1", "Acme", "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111", "submitted"),
        ("p2", "Acme", "https://jobs.ashbyhq.com/acme/22222222-2222-2222-2222-222222222222", "ready"),
    ])
    conn.execute("INSERT INTO applications VALUES ('p1')")
    assert posting_already_applied(conn, "p2", conn.execute(
        "SELECT url FROM postings WHERE posting_id='p2'"
    ).fetchone()[0]) is False


def test_wrapper_and_direct_url_for_same_job_share_one_key() -> None:
    direct = "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111/application"
    wrapped = direct + "?embed=true"
    assert canonical_posting_key("a", direct) == canonical_posting_key("b", wrapped)
```

Update `test_two_ready_rows_for_one_company_submit_only_once` so two distinct canonical job IDs both submit. Add a separate same-job mirror test that permits only one adapter call.

- [ ] **Step 2: Run tests and confirm the company-wide behavior fails**

Run: `python3 -m pytest -q tests/test_submission_identity.py test_submit.py -k 'company or canonical or ledger'`

Expected: FAIL because company-wide suppression still exists.

- [ ] **Step 3: Implement canonical identity**

```python
# submission/identity.py
import hashlib
import sqlite3
from apply.jd import canonical_application_url


def canonical_posting_key(posting_id: str, url: str) -> str:
    canonical = canonical_application_url(url).split("#", 1)[0].rstrip("/")
    material = canonical or posting_id
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    wanted = canonical_posting_key(posting_id, url)
    rows = conn.execute(
        "SELECT p.posting_id,p.url FROM applications a JOIN postings p USING(posting_id)"
    ).fetchall()
    return any(canonical_posting_key(row[0], row[1]) == wanted for row in rows)
```

Replace `_company_already_applied` checks and the same-company active claim query with canonical-posting checks. Preserve `BEGIN IMMEDIATE`, status compare-and-set, and the applications primary key.

- [ ] **Step 4: Update sprint to use canonical posting claims**

Pass `r["url"]` into the shared claim helper. Do not retain any same-company terminal path.

- [ ] **Step 5: Run the complete submission test slice**

Run: `python3 -m pytest -q test_submit.py tests/test_submission_identity.py test_throughput.py`

Expected: PASS with distinct-company-role tests and same-posting mirror tests.

- [ ] **Step 6: Commit**

```bash
git add submission/identity.py tests/test_submission_identity.py submit.py sprint.py test_submit.py
git commit -m "Allow distinct applications at one company"
```

### Task 3: Define ATS lanes and route unsupported rows before tailoring

**Files:**
- Create: `submission/lanes.py`
- Create: `tests/test_submission_lanes.py`
- Modify: `drip.py:21-33,61-71,227-290`
- Modify: `submit_worker.py:19-48`
- Modify: `test_throughput.py:146-184`

**Interfaces:**
- Produces: `LanePolicy(name: str, ats: frozenset[str], concurrency: int, attempts_per_cycle: int, automatic: bool)`
- Produces: `lane_for(ats: str) -> LanePolicy`
- Produces: `classify_url(url: str) -> tuple[str, LanePolicy]`
- Produces: `quarantine_unsupported(conn: sqlite3.Connection) -> int`
- Consumes: `apply.jd.detect_ats`

- [ ] **Step 1: Write failing lane tests**

```python
# tests/test_submission_lanes.py
from submission.lanes import classify_url


def test_lane_mapping_is_explicit() -> None:
    assert classify_url("https://boards.greenhouse.io/acme/jobs/1")[1].name == "direct"
    assert classify_url("https://acme.wd1.myworkdayjobs.com/job/1")[1].name == "workday"
    assert classify_url("https://jobs.ashbyhq.com/acme/id")[1].name == "ashby"
    assert classify_url("https://jobs.smartrecruiters.com/acme/1")[1].automatic is False
    assert classify_url("https://example.com/careers/1")[1].name == "unsupported"
```

Add an in-memory test proving `quarantine_unsupported` moves an unknown queued row to `manual` with `last_error='no adapter for other'` while leaving a Greenhouse row queued.

- [ ] **Step 2: Run tests and confirm missing interfaces**

Run: `python3 -m pytest -q tests/test_submission_lanes.py`

Expected: FAIL because `submission.lanes` does not exist.

- [ ] **Step 3: Implement immutable lane policies**

```python
# submission/lanes.py
from dataclasses import dataclass
from apply.jd import detect_ats

@dataclass(frozen=True)
class LanePolicy:
    name: str
    ats: frozenset[str]
    concurrency: int
    attempts_per_cycle: int
    automatic: bool

DIRECT = LanePolicy("direct", frozenset({"greenhouse", "lever", "workable", "rippling"}), 2, 8, True)
WORKDAY = LanePolicy("workday", frozenset({"workday"}), 1, 2, True)
ASHBY = LanePolicy("ashby", frozenset({"ashby"}), 1, 1, False)
MANUAL = LanePolicy("manual", frozenset({"smartrecruiters"}), 0, 0, False)
UNSUPPORTED = LanePolicy("unsupported", frozenset({"other", "icims"}), 0, 0, False)
POLICIES = (DIRECT, WORKDAY, ASHBY, MANUAL, UNSUPPORTED)


def lane_for(ats: str) -> LanePolicy:
    return next((policy for policy in POLICIES if ats in policy.ats), UNSUPPORTED)


def classify_url(url: str) -> tuple[str, LanePolicy]:
    ats = detect_ats(url)
    return ats, lane_for(ats)
```

Implement `quarantine_unsupported` with status compare-and-set and a normalized `last_error`. Do not move SmartRecruiters rows here because its adapter may still produce an explicit CAPTCHA handoff.

- [ ] **Step 4: Invoke classification before `drip` calls `tailor`**

At the start of each drip tailoring phase, quarantine rows whose lane is `unsupported`. Change `pick_next` to return only rows in automatic or explicitly preparable lanes. Add `workable` to the supported set so the existing adapter remains reachable.

- [ ] **Step 5: Run throughput and lane tests**

Run: `python3 -m pytest -q tests/test_submission_lanes.py test_throughput.py test_submit.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add submission/lanes.py tests/test_submission_lanes.py drip.py submit_worker.py test_throughput.py
git commit -m "Partition submission work by ATS lane"
```

### Task 4: Extract one-posting execution from the global submit loop

**Files:**
- Create: `submission/executor.py`
- Create: `tests/test_submission_executor.py`
- Modify: `submit.py:139-228,251-262,284-584`
- Modify: `sprint.py:131-180`

**Interfaces:**
- Produces: `execute_claimed_posting(conn, row: sqlite3.Row, *, lane: LanePolicy, dry_run: bool, worker_id: str) -> dict`
- Consumes: `submit._isolated_adapter`, `_posting_dead`, `_resume_quality_ready`, `_enforce_submission_safety`, `_mark_outcome`
- Consumes: Task 1 attempt ledger and Task 2 canonical claim

- [ ] **Step 1: Write a failing executor test with a fake adapter**

```python
# tests/test_submission_executor.py

def test_executor_records_manual_attempt_and_releases_other_lanes(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "manual",
        "detected_ats": "greenhouse",
        "reason": "needs answers: ['Current location']",
        "unanswered": ["Current location"],
        "click_attempted": False,
    })
    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")
    assert result["outcome"] == "manual"
    assert conn.execute("SELECT status FROM postings WHERE posting_id=?", (row["posting_id"],)).fetchone()[0] == "manual"
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 1
```

Provide test helpers that create `postings`, `applications`, `emails`, quality metadata, and a minimal one-page fixture PDF. Add cases for confirmed, retryable-before-click, uncertain-after-click, definitive rejection, and stale posting.

- [ ] **Step 2: Run tests and confirm the executor is missing**

Run: `python3 -m pytest -q tests/test_submission_executor.py`

Expected: FAIL because `submission.executor` does not exist.

- [ ] **Step 3: Implement one-posting execution**

Move the body of `submit_ready` from quality check through result persistence into `execute_claimed_posting`. Generate `attempt_id = uuid.uuid4().hex`, call `start_attempt` immediately before adapter launch, and always call `finish_attempt` in `finally` after normalizing the outcome. Keep the existing submit-click uncertainty behavior unchanged.

Expose the subprocess seam as:

```python
def run_adapter(payload: dict) -> dict:
    import submit
    return submit._isolated_adapter(payload)
```

This seam lets tests replace the external browser without patching subprocess internals.

- [ ] **Step 4: Reduce `submit_ready` to selection, claim, execute, and result collection**

Keep `submit_ready(limit=..., dry_run=...)` as a compatibility wrapper for tests, sprint, and manual dry runs. It must count all attempted rows against `limit`, not only confirmed submissions.

- [ ] **Step 5: Route sprint through the shared executor**

After sprint tailoring and claim, call `execute_claimed_posting` with the detected lane. Preserve `notes='sprint'` only when inserting the confirmed application ledger row.

- [ ] **Step 6: Run the full submission tests**

Run: `python3 -m pytest -q test_submit.py tests/test_submission_executor.py tests/test_submission_attempts.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add submission/executor.py tests/test_submission_executor.py submit.py sprint.py
git commit -m "Extract single-posting submission executor"
```

### Task 5: Add the concurrent, always-on dispatcher daemon

**Files:**
- Create: `submission/dispatcher.py`
- Create: `submit_daemon.py`
- Create: `tests/test_submission_dispatcher.py`
- Modify: `launchd/com.jobhunt.submit.plist`
- Create: `deploy/systemd/jobhunt-submit.service`
- Delete: `deploy/systemd/jobhunt@submit.timer`
- Modify: `test_aws_deploy.py:67-76`
- Modify: `test_throughput.py:175-184`

**Interfaces:**
- Produces: `dispatch_cycle(db_path: Path = DB, *, dry_run: bool = False) -> list[dict]`
- Produces: `run_forever(*, poll_seconds: float = 30.0, stop_event: threading.Event | None = None) -> None`
- Consumes: Task 3 lane policies and Task 4 executor

- [ ] **Step 1: Write failing isolation and concurrency tests**

```python
# tests/test_submission_dispatcher.py

def test_blocked_ashby_does_not_block_direct_lane(dispatch_db, monkeypatch) -> None:
    seed_ready(dispatch_db, "ashby-1", "https://jobs.ashbyhq.com/acme/id")
    seed_ready(dispatch_db, "gh-1", "https://boards.greenhouse.io/acme/jobs/1")
    calls = []
    monkeypatch.setattr("submission.dispatcher.execute", lambda posting_id, lane, **kw: calls.append((posting_id, lane.name)) or {"outcome": "submitted"})
    results = dispatch_cycle(dispatch_db, dry_run=False)
    assert ("gh-1", "direct") in calls
    assert all(posting_id != "ashby-1" for posting_id, _ in calls)


def test_direct_lane_never_exceeds_two_workers(dispatch_db, monkeypatch) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    def fake_execute(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"outcome": "submitted"}
    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    seed_many_greenhouse(dispatch_db, count=6)
    dispatch_cycle(dispatch_db)
    assert peak == 2
```

Add tests that Workday concurrency remains one and each attempted failure consumes one `attempts_per_cycle` slot.

- [ ] **Step 2: Run tests and confirm the dispatcher is missing**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py`

Expected: FAIL because `submission.dispatcher` does not exist.

- [ ] **Step 3: Implement one dispatch cycle**

Use one `ThreadPoolExecutor` per automatic lane. Fetch eligible row IDs, atomically claim each row using a fresh SQLite connection, then execute it. Never share a SQLite connection across threads. Ashby remains `automatic=False` until the Ashby recovery plan enables it.

```python
def dispatch_cycle(db_path=DB, *, dry_run=False):
    policies = [DIRECT, WORKDAY]
    results = []
    with ThreadPoolExecutor(max_workers=sum(p.concurrency for p in policies)) as pool:
        futures = []
        for policy in policies:
            for posting_id in select_for_lane(db_path, policy, policy.attempts_per_cycle):
                futures.append(pool.submit(execute_by_id, db_path, posting_id, policy, dry_run))
        for future in as_completed(futures):
            results.append(future.result())
    return results
```

- [ ] **Step 4: Implement the daemon loop and compatibility CLI**

`submit_daemon.py --once --dry-run` runs one safe cycle. Normal launchd execution calls `run_forever`, catches cycle-level exceptions, logs them, waits 30 seconds, and continues. A stop event makes the loop deterministic in tests.

- [ ] **Step 5: Replace the 65-minute launchd timer with KeepAlive**

Set `ProgramArguments` to `/usr/bin/python3 /Users/davidcui824/jobhunt/submit_daemon.py`. Remove `StartInterval`. Add:

```xml
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>ProcessType</key><string>Background</string>
```

Create `deploy/systemd/jobhunt-submit.service` with `ExecStart=/opt/jobhunt/.venv/bin/python /opt/jobhunt/submit_daemon.py`, `Restart=always`, and `RestartSec=10`. Delete `deploy/systemd/jobhunt@submit.timer`. Update `test_aws_deploy.py` to expect five remaining timer files and to assert the dedicated submit service has restart supervision.

- [ ] **Step 6: Update throughput tests**

Replace timer assertions with checks that launchd uses `KeepAlive`, calls `submit_daemon.py`, and has no `StartInterval`. Assert the dispatcher poll interval default is 30 seconds.

- [ ] **Step 7: Run dispatcher, launch config, and full tests**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py test_throughput.py test_aws_deploy.py`

Run: `plutil -lint launchd/com.jobhunt.submit.plist`

Run: `systemd-analyze verify deploy/systemd/jobhunt-submit.service` when `systemd-analyze` is available; otherwise run the repository's text-based unit checks.

Expected: all available checks PASS.

- [ ] **Step 8: Commit**

```bash
git add submission/dispatcher.py submit_daemon.py tests/test_submission_dispatcher.py \
  launchd/com.jobhunt.submit.plist deploy/systemd/jobhunt-submit.service \
  test_aws_deploy.py test_throughput.py
git rm deploy/systemd/jobhunt@submit.timer
git commit -m "Run ATS-partitioned submit dispatcher continuously"
```

### Task 6: Add per-ATS metrics to the digest and complete the local rollout

**Files:**
- Create: `submission/metrics.py`
- Create: `tests/test_submission_metrics.py`
- Modify: `digest.py:68-162`
- Modify: `docs/system-design.html`
- Modify: `README.md`

**Interfaces:**
- Produces: `attempt_metrics(conn, *, since: int) -> list[dict]`
- Produces: `queue_metrics(conn) -> list[dict]`
- Consumes: `submission_attempts`, `postings`, and lane classification

- [ ] **Step 1: Write failing metrics tests**

```python
# tests/test_submission_metrics.py

def test_attempt_metrics_group_by_ats_and_outcome(metrics_db) -> None:
    seed_attempt(metrics_db, ats="greenhouse", outcome="submitted", duration_ms=1000)
    seed_attempt(metrics_db, ats="greenhouse", outcome="manual", duration_ms=3000)
    rows = attempt_metrics(metrics_db, since=0)
    greenhouse = next(row for row in rows if row["ats"] == "greenhouse")
    assert greenhouse["attempts"] == 2
    assert greenhouse["confirmed"] == 1
    assert greenhouse["confirmation_rate"] == 0.5
```

Add a digest composition test expecting compact text such as `greenhouse 1/2 confirmed` and an Ashby breaker status line only when that lane is paused.

- [ ] **Step 2: Run tests and confirm missing metrics**

Run: `python3 -m pytest -q tests/test_submission_metrics.py tests/test_digest_phone.py`

Expected: FAIL because metrics are not collected.

- [ ] **Step 3: Implement aggregate queries**

Use SQL grouping for counts and Python for p50 and p95 duration. Never read raw answer-bank values or screenshot contents. Normalize unknown ATS as `other`.

- [ ] **Step 4: Add a compact operational digest section**

Append only the top failing ATSs and current queue depth by lane. Keep the Telegram body under `SHORT_LIMIT`. Do not restore per-item email notifications.

- [ ] **Step 5: Update architecture documentation**

Document the continuous dispatcher, ATS lanes, canonical posting deduplication, and attempt ledger. Remove claims that one global hourly submit loop is the active architecture.

- [ ] **Step 6: Run the whole suite and dry-run the daemon**

Run: `python3 -m pytest -q`

Run: `python3 submit_daemon.py --once --dry-run`

Expected: all tests PASS; dry-run prints selected rows but performs no external click and leaves statuses unchanged.

- [ ] **Step 7: Install the launchd update with rollback capture**

```bash
cp "$HOME/Library/LaunchAgents/com.jobhunt.submit.plist" \
  "$HOME/Library/LaunchAgents/com.jobhunt.submit.plist.pre-dispatcher"
cp launchd/com.jobhunt.submit.plist "$HOME/Library/LaunchAgents/com.jobhunt.submit.plist"
launchctl bootout "gui/$(id -u)/com.jobhunt.submit" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.jobhunt.submit.plist"
launchctl kickstart -k "gui/$(id -u)/com.jobhunt.submit"
launchctl print "gui/$(id -u)/com.jobhunt.submit"
```

Verify one daemon PID, no visible window, and no automatic Ashby attempt.

- [ ] **Step 8: Commit**

```bash
git add submission/metrics.py tests/test_submission_metrics.py digest.py \
  docs/system-design.html README.md
git commit -m "Report per-ATS submission health"
```

### Task 7: Remove long-lived secrets from launchd plists

**Files:**
- Create: `runtime_secrets.py`
- Create: `scripts/migrate_launchd_secrets.py`
- Create: `tests/test_runtime_secrets.py`
- Modify: `launchd/com.jobhunt.drip.plist`
- Modify: `launchd/com.jobhunt.inbox.plist`
- Modify: `launchd/com.jobhunt.revise.plist`
- Modify: `launchd/com.jobhunt.sprint.plist`
- Modify: `launchd/com.jobhunt.submit.plist`
- Modify: `README.md`

**Interfaces:**
- Produces: `load_runtime_env(path: Path) -> dict[str, str]`
- Produces CLI: `python3 runtime_secrets.py -- <command> [args...]`
- Produces CLI: `python3 scripts/migrate_launchd_secrets.py --launch-agents DIR --output PATH`

- [ ] **Step 1: Write failing secret-wrapper tests**

```python
# tests/test_runtime_secrets.py
import os
import plistlib
import stat

import pytest

from runtime_secrets import load_runtime_env
from scripts.migrate_launchd_secrets import migrate


def test_runtime_env_requires_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "runtime.env"
    path.write_text("ANTHROPIC_API_KEY=fake-secret\n")
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_runtime_env(path)


def test_migration_writes_mode_600_without_printing_values(tmp_path, capsys) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    plist = {"EnvironmentVariables": {"ANTHROPIC_API_KEY": "fake-secret", "PATH": "/usr/bin"}}
    with (agents / "com.jobhunt.drip.plist").open("wb") as handle:
        plistlib.dump(plist, handle)
    output = tmp_path / "runtime.env"
    migrate(agents, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "fake-secret" in output.read_text()
    assert "fake-secret" not in capsys.readouterr().out
```

Add tests that unknown keys are rejected, malformed lines fail closed, the wrapper never logs values, and every committed jobhunt plist contains no `ANTHROPIC_API_KEY` value.

- [ ] **Step 2: Run tests and confirm the wrapper is missing**

Run: `python3 -m pytest -q tests/test_runtime_secrets.py`

Expected: FAIL because `runtime_secrets.py` and the migration script do not exist.

- [ ] **Step 3: Implement secure env loading and exec**

Use `~/.config/jobhunt/runtime.env` by default. Require the current user as owner and mode `0600`. Parse only allowlisted keys, merge them into a copy of `os.environ`, and invoke the requested command with `os.execvpe`. Never print values or pass them as command-line arguments.

- [ ] **Step 4: Implement one-time plist migration**

Read the installed jobhunt plists with `plistlib`, collect allowlisted secret values in memory, require identical values across plists, write the secure env file atomically with mode `0600`, and print only migrated key names. Do not modify installed plists in this step.

- [ ] **Step 5: Remove secrets from committed plists and add the wrapper**

Change each jobhunt plist to launch:

Use `/usr/bin/python3 /Users/davidcui824/jobhunt/runtime_secrets.py -- /usr/bin/python3` as the command prefix. Preserve each exact script as the final argument: `drip.py`, `inbox.py`, `revise.py`, `sprint.py`, and `submit_daemon.py` respectively.

Retain non-secret `PATH` and operational flags in `EnvironmentVariables`. Remove every long-lived key value.

- [ ] **Step 6: Validate and install the sanitized plists**

```bash
python3 scripts/migrate_launchd_secrets.py \
  --launch-agents "$HOME/Library/LaunchAgents" \
  --output "$HOME/.config/jobhunt/runtime.env"
for f in launchd/*.plist; do plutil -lint "$f"; done
python3 -m pytest -q tests/test_runtime_secrets.py
```

Back up installed plists, copy the sanitized versions, bootstrap them, and verify each job starts without exposing the secret in `launchctl print`. Do not print the secure env file.

- [ ] **Step 7: Document provider-side key rotation as a separate approval**

The existing key has been present in plist and Git history. Document that revoking it and issuing a replacement is recommended, but do not revoke or replace it without explicit approval.

- [ ] **Step 8: Commit**

```bash
git add runtime_secrets.py scripts/migrate_launchd_secrets.py \
  tests/test_runtime_secrets.py launchd README.md
git commit -m "Remove secrets from launchd configuration"
```

## Plan 1 Completion Gate

Run all of the following before starting the Ashby recovery plan:

```bash
python3 -m pytest -q
python3 submit_daemon.py --once --dry-run
plutil -lint launchd/com.jobhunt.submit.plist
git diff --check
git status --short
```

Required observations:

- The complete suite passes.
- A Greenhouse or Lever row can execute while Ashby is disabled.
- Distinct roles at one company are not suppressed.
- The same canonical posting cannot execute twice.
- Every attempted fake-adapter path creates exactly one finished attempt row.
- `out/tracker.db` is modified only by the live service and is not staged.
