# Ashby Persistent Browser and Canary Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Ashby's ineffective fresh spoofed browser sessions with one persistent, hidden, real Chrome profile and a measured canary policy that stops on the first trust failure.

**Architecture:** Ashby receives a dedicated browser backend and persistent local profile. Its eligibility and rate state live in SQLite, separate from generic ATS pacing. The local dispatcher asks the Ashby policy for one eligible canary, records the result, and advances or rolls back a fixed cadence tier. Other ATS adapters continue using their current browser path until independently justified.

**Tech Stack:** Python 3.9, Playwright persistent context, Chrome for Testing or stable Chrome, macOS System Events, SQLite, existing submission dispatcher and attempt ledger

**Spec:** `docs/superpowers/specs/2026-08-22-hybrid-pipeline-ashby-recovery-design.md`

## Global Constraints

- Complete `2026-08-22-local-ats-dispatcher-plan.md` first.
- Spam-rejected or unconfirmed Ashby rows are never automatically retried.
- The first enabled cycle may perform at most one live Ashby attempt.
- Use a previously unattempted, eligible posting for every canary.
- Preserve the actual browser user agent, plugins, GPU, language, timezone, and hardware values.
- Do not use rotating proxies, CAPTCHA solvers, fabricated identity values, or DeskPad.
- Headful rendering must remain invisible and must not hide David's normal Chrome windows.
- Browser profile state remains local and must never enter Git, S3, logs, or screenshots.
- Do not stage or commit `out/tracker.db` or browser profile files.

---

### Task 1: Add persistent ATS lane state and deterministic Ashby policy transitions

**Files:**
- Create: `submission/ashby_policy.py`
- Create: `tests/test_ashby_policy.py`
- Modify: `submission/database.py`
- Modify: `submission/lanes.py`

**Interfaces:**
- Produces: `ensure_lane_state(conn: sqlite3.Connection) -> None`
- Produces: `AshbyState(enabled: bool, tier: int, consecutive_confirmed: int, next_attempt_at: int, blocked_until: int, last_outcome: str)`
- Produces: `load_state(conn: sqlite3.Connection) -> AshbyState`
- Produces: `can_attempt(state: AshbyState, *, now: int) -> bool`
- Produces: `record_result(conn: sqlite3.Connection, *, outcome: str, reason: str, now: int) -> AshbyState`
- Produces: `set_enabled(conn: sqlite3.Connection, enabled: bool, *, now: int) -> AshbyState`
- Consumes: the Task 1 SQLite connection helper

- [ ] **Step 1: Write failing state-machine tests**

```python
# tests/test_ashby_policy.py
import sqlite3

from submission.ashby_policy import (
    can_attempt,
    ensure_lane_state,
    load_state,
    record_result,
    set_enabled,
)


def policy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_lane_state(conn)
    return conn


def test_ashby_starts_disabled() -> None:
    state = load_state(policy_db())
    assert state.enabled is False
    assert can_attempt(state, now=1_000) is False


def test_three_confirmations_advance_from_180_to_90_minutes() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=1_000)
    assert state.next_attempt_at == 1_000 + 180 * 60
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    assert state.tier == 1
    assert state.next_attempt_at == 1_000 + 180 * 60 * 2 + 90 * 60


def test_spam_rejection_blocks_24_hours_and_slows_one_tier() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    for now in (1_000, 11_800, 22_600):
        record_result(conn, outcome="submitted", reason="confirmed", now=now)
    state = record_result(conn, outcome="manual", reason="flagged as possible spam", now=30_000)
    assert state.blocked_until == 30_000 + 24 * 60 * 60
    assert state.tier == 0
    assert state.consecutive_confirmed == 0
    assert can_attempt(state, now=state.blocked_until - 1) is False
```

Add tests for 10 confirmations advancing to the 45-minute tier, an unconfirmed click pausing the lane, and a generic unanswered-question result leaving the cadence tier unchanged while requiring a new posting.

- [ ] **Step 2: Run tests and confirm the state module is missing**

Run: `python3 -m pytest -q tests/test_ashby_policy.py`

Expected: FAIL because `submission.ashby_policy` does not exist.

- [ ] **Step 3: Implement the lane-state schema**

```sql
CREATE TABLE IF NOT EXISTS ats_lane_state (
    ats TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    tier INTEGER NOT NULL DEFAULT 0,
    consecutive_confirmed INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    blocked_until INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT NOT NULL DEFAULT '',
    last_reason TEXT NOT NULL DEFAULT '',
    policy_revision TEXT NOT NULL DEFAULT 'ashby-canary-v1',
    updated_at INTEGER NOT NULL
);
```

Insert the `ashby` row with `enabled=0`. Never infer enabled state from the old `out/ashby_cooldown` file.

- [ ] **Step 4: Implement exact policy transitions**

Use interval tiers `(180, 90, 45)` minutes. A confirmed submission increments the streak and advances at streaks 3 and 10. A spam reason sets `blocked_until = now + 86400`, resets the streak, and decreases the tier by one without going below zero. An unconfirmed outcome sets `enabled=0`. `needs answers` does not count as a browser-trust failure.

- [ ] **Step 5: Run focused and lane tests**

Run: `python3 -m pytest -q tests/test_ashby_policy.py tests/test_submission_lanes.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add submission/ashby_policy.py tests/test_ashby_policy.py \
  submission/database.py submission/lanes.py
git commit -m "Add Ashby canary state machine"
```

### Task 2: Resolve a dedicated Chrome binary and hide only its process

**Files:**
- Create: `apply/ashby_browser.py`
- Create: `apply/hide_macos_browser.py`
- Create: `tests/test_ashby_browser.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `resolve_chrome() -> ChromeTarget`
- Produces: `ChromeTarget(executable: Path, process_name: str, dedicated: bool)`
- Produces: `persistent_ashby_context(pw, *, profile_dir: Path = PROFILE_DIR) -> ContextManager[BrowserContext]`
- Produces: `start_hide_watchdog(target: ChromeTarget, *, timeout_seconds: float = 5.0) -> subprocess.Popen`

- [ ] **Step 1: Write failing browser resolution and ordering tests**

```python
# tests/test_ashby_browser.py
from pathlib import Path
from unittest import mock

from apply.ashby_browser import resolve_chrome


def test_chrome_for_testing_is_preferred(tmp_path: Path, monkeypatch) -> None:
    testing = tmp_path / "Google Chrome for Testing"
    testing.write_text("")
    monkeypatch.setenv("JOBHUNT_ASHBY_CHROME_PATH", str(testing))
    target = resolve_chrome()
    assert target.executable == testing
    assert target.process_name == "Google Chrome for Testing"
    assert target.dedicated is True


def test_hide_watchdog_starts_before_browser_launch(monkeypatch, tmp_path: Path) -> None:
    events = []
    monkeypatch.setattr("apply.ashby_browser.start_hide_watchdog", lambda *a, **k: events.append("watchdog"))
    fake_pw = fake_playwright(events)
    with persistent_ashby_context(fake_pw, profile_dir=tmp_path / "profile"):
        pass
    assert events[:2] == ["watchdog", "launch"]
```

Add a test that stable Google Chrome is rejected when it is already running unless `JOBHUNT_ASHBY_ALLOW_SHARED_CHROME=1`, because hiding it could affect the user's windows.

- [ ] **Step 2: Run tests and confirm the browser module is missing**

Run: `python3 -m pytest -q tests/test_ashby_browser.py`

Expected: FAIL because `apply.ashby_browser` does not exist.

- [ ] **Step 3: Implement deterministic browser resolution**

Resolution order:

1. `JOBHUNT_ASHBY_CHROME_PATH`
2. `/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`
3. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`

Fail with a clear error if no binary exists. For ordinary Chrome, check `pgrep -x 'Google Chrome'`; fail closed when it is already running unless the explicit shared-Chrome override is set.

- [ ] **Step 4: Implement the process-scoped watchdog**

`apply/hide_macos_browser.py` must poll System Events for exactly `process_name`, set only that process's `visible` property to false, and exit after the timeout. It must not hide `Google Chrome` when the target is `Google Chrome for Testing`.

Use this AppleScript body:

```applescript
on run argv
  set processName to item 1 of argv
  tell application "System Events"
    if exists process processName then
      set visible of process processName to false
      return "hidden"
    end if
  end tell
  return "waiting"
end run
```

Pass `processName` as an escaped script argument rather than interpolating raw shell text.

- [ ] **Step 5: Implement the persistent context manager**

Launch with `headless=False`, `executable_path=target.executable`, and `user_data_dir=profile_dir`. Do not pass `user_agent`, `locale`, WebGL overrides, plugin overrides, hardware overrides, or `_STEALTH_JS`. Add `.jobhunt-browser-profiles/` and `out/browser-profiles/` to `.gitignore`.

- [ ] **Step 6: Run focused tests and a no-navigation local smoke check**

Run: `python3 -m pytest -q tests/test_ashby_browser.py`

Run a smoke helper that launches the dedicated browser to `about:blank`, verifies the context's profile directory exists, verifies the process becomes hidden, closes it, and makes no network request to an ATS.

Expected: PASS; no ordinary Chrome process is hidden.

- [ ] **Step 7: Commit**

```bash
git add apply/ashby_browser.py apply/hide_macos_browser.py \
  tests/test_ashby_browser.py .gitignore
git commit -m "Add persistent hidden Ashby browser profile"
```

### Task 3: Move only Ashby off the generic spoofed browser launcher

**Files:**
- Modify: `apply/ashby.py:13-18,75-182`
- Modify: `apply/stealth.py:1-88`
- Modify: `tests/test_ashby.py`
- Modify: `test_submit.py:69-92`

**Interfaces:**
- Consumes: `persistent_ashby_context`
- Preserves: `apply_ashby(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict`

- [ ] **Step 1: Write a failing test that Ashby uses the persistent backend**

```python

def test_apply_ashby_uses_persistent_context(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(ashby, "persistent_ashby_context", fake_context(calls))
    result = ashby.apply_ashby(
        "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
        tmp_path / "resume.pdf",
        "acme",
        dry_run=True,
    )
    assert calls == ["enter", "exit"]
    assert result["submitted"] is False
```

Use a fake page that exercises upload, basics, QA extraction, screenshot, and dry-run return without a real network request.

- [ ] **Step 2: Run the focused test and confirm it fails on `launch_stealth_context`**

Run: `python3 -m pytest -q tests/test_ashby.py -k persistent`

Expected: FAIL because Ashby still calls the generic launcher.

- [ ] **Step 3: Switch Ashby to the context manager**

Replace:

```python
browser, ctx = stealth.launch_stealth_context(pw)
page = configure_page(ctx.new_page())
```

with:

```python
with persistent_ashby_context(pw) as ctx:
    page = configure_page(ctx.pages[0] if ctx.pages else ctx.new_page())
    return _run_ashby_form(page, url, resume_pdf, slug, dry_run)
```

Extract `_run_ashby_form` so the form logic remains unit-testable. Close only the Ashby context. Do not call `browser.close()` inside the form helper.

- [ ] **Step 4: Correct generic stealth documentation**

Remove claims that the generic fingerprint hardening solves Ashby. State that the module remains a compatibility launcher for other adapters and that Ashby uses its dedicated persistent backend.

- [ ] **Step 5: Run Ashby and submission safety tests**

Run: `python3 -m pytest -q tests/test_ashby.py tests/test_ashby_browser.py test_submit.py -k 'ashby or spam or unconfirmed'`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apply/ashby.py apply/stealth.py tests/test_ashby.py test_submit.py
git commit -m "Use persistent real Chrome for Ashby"
```

### Task 4: Integrate Ashby eligibility and outcomes with the dispatcher

**Files:**
- Modify: `submission/dispatcher.py`
- Modify: `submission/executor.py`
- Create: `manage_lanes.py`
- Create: `tests/test_ashby_dispatch.py`
- Modify: `digest.py`

**Interfaces:**
- Produces: `eligible_ashby_posting(conn, *, now: int) -> str | None`
- Produces CLI: `python3 manage_lanes.py status ashby`
- Produces CLI: `python3 manage_lanes.py enable-canary ashby`
- Produces CLI: `python3 manage_lanes.py pause ashby`
- Consumes: Ashby state machine and attempt outcomes

- [ ] **Step 1: Write failing dispatcher-policy tests**

```python
# tests/test_ashby_dispatch.py

def test_one_enabled_cycle_claims_at_most_one_unattempted_ashby(ashby_db, monkeypatch) -> None:
    seed_ready_ashby(ashby_db, count=3)
    enable_ashby(ashby_db, now=1_000)
    calls = []
    monkeypatch.setattr("submission.dispatcher.execute_by_id", lambda *args, **kwargs: calls.append(args[1]) or {"outcome": "submitted", "reason": "confirmed"})
    dispatch_cycle(ashby_db, now=1_000)
    assert len(calls) == 1


def test_spam_result_blocks_next_cycle_without_blocking_greenhouse(ashby_db, monkeypatch) -> None:
    seed_ready_ashby(ashby_db, count=2)
    seed_ready_greenhouse(ashby_db, count=1)
    enable_ashby(ashby_db, now=1_000)
    fake_results(monkeypatch, ashby="possible spam", greenhouse="confirmed")
    dispatch_cycle(ashby_db, now=1_000)
    calls = capture_calls(monkeypatch)
    dispatch_cycle(ashby_db, now=2_000)
    assert calls.count("ashby") == 1
    assert calls.count("greenhouse") >= 1
```

Add a test that a posting with any completed `submission_attempts` row whose reason is definitive spam or whose click was uncertain cannot be selected as a canary.

- [ ] **Step 2: Run tests and confirm Ashby remains disconnected**

Run: `python3 -m pytest -q tests/test_ashby_dispatch.py`

Expected: FAIL because the dispatcher never consults Ashby state.

- [ ] **Step 3: Add Ashby candidate selection**

Select the oldest eligible, never-attempted Ashby posting with a valid quality artifact. Exclude every posting with a confirmed application ledger row, definitive rejection attempt, or unconfirmed-click attempt. Claim one row atomically.

- [ ] **Step 4: Record the lane transition after every Ashby result**

Map `submitted + confirmed` to success, `possible spam` to breaker, `submission_uncertain` to pause, and pre-click `needs answers` to a neutral trust result. Update policy state only after the attempt ledger is durable.

- [ ] **Step 5: Add the management CLI**

The CLI must use SQLite state and print JSON. `enable-canary` sets `enabled=1`, `next_attempt_at=now`, and leaves tier zero. It does not launch the dispatcher or submit by itself. `pause` is reversible.

- [ ] **Step 6: Add concise Telegram state**

When Ashby is enabled or blocked, include tier, next eligible time, consecutive confirmations, and ready depth. Do not include this section when the lane is paused and empty.

- [ ] **Step 7: Run policy, dispatcher, and digest tests**

Run: `python3 -m pytest -q tests/test_ashby_policy.py tests/test_ashby_dispatch.py tests/test_submission_dispatcher.py tests/test_digest_phone.py`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add submission/dispatcher.py submission/executor.py manage_lanes.py \
  tests/test_ashby_dispatch.py digest.py
git commit -m "Connect Ashby canary policy to dispatcher"
```

### Task 5: Validate hidden rendering and release exactly one canary

**Files:**
- Create: `scripts/verify_ashby_browser.py`
- Modify: `README.md`
- Modify: `docs/system-design.html`

**Interfaces:**
- Produces CLI: `python3 scripts/verify_ashby_browser.py --about-blank`
- Consumes: browser resolver, hide watchdog, dispatcher dry-run, management CLI

- [ ] **Step 1: Add a non-ATS browser verification script**

The script launches the dedicated profile to `about:blank`, prints executable path, process name, profile path, `navigator.userAgent`, `navigator.webdriver`, plugin count, and visibility result. It must redact the profile's file contents and close without visiting any ATS.

- [ ] **Step 2: Run the complete offline verification**

```bash
python3 -m pytest -q
python3 scripts/verify_ashby_browser.py --about-blank
python3 manage_lanes.py status ashby
python3 submit_daemon.py --once --dry-run
```

Expected:

- all tests PASS
- dedicated browser process is hidden
- ordinary Chrome remains visible and unaffected
- Ashby state is paused
- dry-run performs no external click

- [ ] **Step 3: Capture a database backup and live baseline**

```bash
mkdir -p out/backups
sqlite3 out/tracker.db ".backup 'out/backups/pre-ashby-canary.db'"
sqlite3 out/tracker.db "PRAGMA integrity_check"
python3 manage_lanes.py status ashby
```

Expected: `ok` integrity result and one readable backup file. Do not commit the backup.

- [ ] **Step 4: Select and inspect one canary without clicking**

Run the dispatcher in a canary preview mode that prints posting ID, company, title, canonical URL, prior-attempt count, and quality artifact state. Confirm it is eligible under the user's current role and season filters and has no prior external attempt.

- [ ] **Step 5: Enable and release one canary**

```bash
python3 manage_lanes.py enable-canary ashby
python3 submit_daemon.py --once
python3 manage_lanes.py pause ashby
```

The immediate pause after one cycle is mandatory even if the result confirms. Inspect the attempt row, posting status, screenshot, and Ashby policy state. There must be exactly one new Ashby attempt.

- [ ] **Step 6: Classify the observed result**

- Confirmed: retain the result, set next eligible time to 180 minutes, and re-enable only after the review gate.
- Explicit spam: verify the 24-hour breaker and keep the lane paused.
- Unconfirmed: verify the posting is manual and the lane is paused.
- Needs answers before click: verify no trust failure was counted and route the question to Telegram.

Do not run a second live canary in this task.

- [ ] **Step 7: Update operational docs with the observed path**

Document how to inspect state, pause, enable one canary, restore the pre-canary backup only when no external submission occurred, and never retry a click-uncertain row.

- [ ] **Step 8: Commit documentation and verification script**

```bash
git add scripts/verify_ashby_browser.py README.md docs/system-design.html
git commit -m "Document Ashby canary operations"
```

## Plan 2 Completion Gate

Required evidence:

- Full test suite passes.
- Dedicated browser profile persists across two `about:blank` launches.
- No fabricated UA, plugin, GPU, language, or hardware values are injected for Ashby.
- David's normal Chrome windows remain unaffected.
- One live cycle creates at most one Ashby attempt.
- A spam or unconfirmed result blocks a second automatic attempt.
- The canary attempt and lane transition are present in SQLite telemetry.
- `out/tracker.db`, profile files, backups, and screenshots are not staged.
