# Workday Recovery and Throughput Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair Workday application entry after account recovery, classify missing Apply controls honestly, and double lane throughput without concurrent sessions for one tenant.

**Architecture:** Workday form entry becomes an idempotent helper that reacquires page state after every navigation or recovery. Dispatcher selection uses a pure tenant key to exclude same-tenant overlap, then the lane widens from one to two workers only after focused and live acceptance checks pass.

**Tech Stack:** Python 3.9, Playwright sync API, SQLite, ThreadPoolExecutor, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-jobhunt-blocker-remediation-design.md`

## Global Constraints

- Never reuse a locator created before navigation or account recovery.
- Never declare a posting closed without an observed closed or not-found marker.
- Never retry after a submit click or uncertain result.
- Recovery remains at most once per tenant per policy window.
- One dispatch cycle may run at most one Workday posting per tenant.
- Use separate browser contexts and SQLite connections per worker.
- Start with Workday concurrency 1 and widen only after tenant-exclusion tests and a live observed cycle.
- Use TDD and frequent commits.
- Never stage `out/tracker.db` or credential material.
- Do not use Unicode em or en dashes in added prose.

## File structure

- Create `submission/workday_tenant.py`: pure tenant identity from Workday URLs.
- Modify `submission/dispatcher.py`: exclusion-key selection for Workday.
- Modify `submission/lanes.py`: Workday policy widening after guard acceptance.
- Modify `apply/workday.py`: idempotent form entry, recovery re-entry, and honest Apply-button classification.
- Create `scripts/requeue_workday_recoverable.py`: preview-first covered-failure requeue.
- Modify `test_workday.py`: page-state and result tests.
- Modify `tests/test_submission_dispatcher.py`: tenant exclusion and concurrency tests.
- Create `tests/test_workday_requeue.py`: CLI and safety tests.

---

### Task 1: Pure Workday tenant identity and dispatcher exclusion

**Files:**
- Create: `submission/workday_tenant.py`
- Modify: `submission/dispatcher.py:105-132, 225-264`
- Modify: `tests/test_submission_dispatcher.py`

**Interfaces:**
- Produces: `workday_tenant_key(url: str) -> str`
- Extends: `select_for_lane(..., excluded_keys: set[str] | None = None) -> list[str]`
- Produces: `selection_key(policy, url) -> str | None`

- [ ] **Step 1: Write failing tenant-key tests**

```python
def test_workday_tenant_key_is_host_and_site():
    assert workday_tenant_key(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Role_R123"
    ) == "acme.wd5.myworkdayjobs.com/external"


def test_workday_cycle_never_selects_same_tenant_twice(dispatch_db):
    seed_ready(dispatch_db, "a", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/A_R1")
    seed_ready(dispatch_db, "b", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/B_R2")
    seed_ready(dispatch_db, "c", "https://other.wd5.myworkdayjobs.com/en-US/Jobs/job/C_R3")
    assert select_for_lane(dispatch_db, WORKDAY, limit=4) == ["a", "c"]
```

Cover locale and locale-free URLs, malformed URLs, and two sites on one host.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py -k 'tenant or workday_cycle'`

Expected: FAIL because tenant-key and exclusion behavior do not exist.

- [ ] **Step 3: Implement the pure key**

```python
def workday_tenant_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    site = parts[0].lower() if parts else ""
    return f"{host}/{site}"
```

Return an empty string for non-Workday hosts.

- [ ] **Step 4: Apply exclusion during selection**

In `select_for_lane`, maintain `selected_keys`. For Workday, skip a row when its nonempty tenant key is already present. Add the key only when the posting ID is selected. Direct and other lane order remains unchanged.

- [ ] **Step 5: Prove failure isolation and worker ownership remain intact**

Extend the existing direct/workday failure-isolation test with two tenants and assert separate worker calls, fresh connection behavior, and no same-tenant pair.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py tests/test_submission_lanes.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS with Workday policy still at concurrency 1.

- [ ] **Step 7: Commit**

```bash
git add submission/workday_tenant.py submission/dispatcher.py tests/test_submission_dispatcher.py
git commit -m "Exclude same-tenant Workday overlap"
```

### Task 2: Idempotent Workday form entry after recovery

**Files:**
- Modify: `apply/workday.py:1324-1482`
- Modify: `test_workday.py`

**Interfaces:**
- Produces: `WorkdayEntryResult`
- Produces: `enter_application_form(page, apply_url: str) -> WorkdayEntryResult`
- Consumes: `ensure_workday_account_access` and existing recovery helpers

- [ ] **Step 1: Write failing re-entry tests**

Create a fake page whose first entry returns an authentication gate, account recovery succeeds, and the second fresh navigation exposes Apply, Autofill with Resume, and the upload input.

```python
def test_successful_recovery_reenters_apply_and_autofill(monkeypatch):
    page = RecoveryThenApplicationPage()
    result = run_entry_sequence(page, APPLY_URL)
    assert page.goto_calls == [APPLY_URL, APPLY_URL]
    assert page.apply_clicks == 1
    assert page.autofill_clicks == 1
    assert result.state == "upload_ready"
```

Assert no locator object created before the second `goto` is clicked afterward.

- [ ] **Step 2: Run test and verify RED**

Run: `python3 -m pytest -q test_workday.py -k 'reenters_apply or fresh_locator'`

Expected: FAIL because entry is embedded in `apply_workday` and stale locators are reused.

- [ ] **Step 3: Add a structured entry result**

```python
@dataclass(frozen=True)
class WorkdayEntryResult:
    state: str
    reason: str = ""
    marker: str = ""
```

Allowed states are `upload_ready`, `auth_required`, `closed`, and `retryable`.

- [ ] **Step 4: Extract `enter_application_form`**

The helper must:

1. `goto(apply_url, wait_until='domcontentloaded')`
2. read bounded body text
3. return `closed` only for explicit marker matches
4. reacquire and click `[data-automation-id='adventureButton']`
5. reacquire and click `[data-automation-id='autofillWithResume']`
6. check for the file input or saved-draft wizard
7. return `auth_required` for visible sign-in or create-account gates
8. return `retryable` for all other missing-control states

Every locator is local to one helper call.

- [ ] **Step 5: Re-enter after successful access recovery**

In `apply_workday`, when `ensure_workday_account_access` reports success, call `enter_application_form(page, apply_url)` again. Do not click the previous `af` locator. Continue to upload only from a new `upload_ready` result.

- [ ] **Step 6: Preserve saved-draft behavior**

Add a regression that `saved_draft_wizard_is_active(page)` still bypasses initial upload and refreshes the saved resume through `refresh_saved_resume`.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q test_workday.py -k 'recovery or upload or saved_draft or entry'`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apply/workday.py test_workday.py
git commit -m "Reenter Workday form after recovery"
```

### Task 3: Honest missing-Apply classification and bounded fallback

**Files:**
- Modify: `apply/workday.py:1324-1482`
- Modify: `test_workday.py`
- Modify: `tests/test_submission_executor.py`

**Interfaces:**
- Produces: `workday_closed_marker(body_text: str) -> str | None`
- Produces adapter results with `outcome='stale'` only on evidence and `outcome='retryable_failure'` otherwise

- [ ] **Step 1: Write failing closed and ambiguous tests**

```python
def test_missing_apply_with_closed_marker_is_stale():
    result = entry_result_for_body("This job is no longer available")
    assert result == WorkdayEntryResult("closed", "posting closed", "no longer available")


def test_missing_apply_without_closed_marker_is_retryable():
    result = entry_result_for_body("Welcome to careers")
    assert result.state == "retryable"
```

Add a test that a supported direct application fallback is attempted once and never loops.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q test_workday.py -k 'closed_marker or missing_apply or fallback'`

Expected: FAIL because missing Apply is always a hard failure.

- [ ] **Step 3: Implement explicit closure markers**

```python
CLOSED_MARKERS = (
    "job is no longer available",
    "position is no longer available",
    "no longer accepting applications",
    "job posting has been removed",
    "job not found",
)


def workday_closed_marker(body_text: str) -> str | None:
    text = re.sub(r"\s+", " ", body_text.lower())
    return next((marker for marker in CLOSED_MARKERS if marker in text), None)
```

- [ ] **Step 4: Map structured entry states to safe adapter results**

For `closed`, return `outcome='stale'`, `retryable=False`, `click_attempted=False`, and the observed marker. For `retryable`, return `outcome='retryable_failure'`, `retryable=True`, and no closure claim.

- [ ] **Step 5: Add one direct application fallback**

Only when the current Workday page exposes an explicit application href, resolve that href and call `enter_application_form` once. Record whether the page-provided fallback was used in the reason. Do not append `/apply` or invent a route for unknown tenants.

- [ ] **Step 6: Verify executor bounded retry behavior**

Seed a retryable Workday result at attempt count 0 and assert executor returns posting to `ready`. Seed the same result at attempt count 2 and assert it settles `failed`. Seed a click-attempted result and assert it remains `manual` regardless of retryable flags.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q test_workday.py tests/test_submission_executor.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apply/workday.py test_workday.py tests/test_submission_executor.py
git commit -m "Classify Workday entry failures honestly"
```

### Task 4: Preview-first Workday recoverable-row requeue

**Files:**
- Create: `scripts/requeue_workday_recoverable.py`
- Create: `tests/test_workday_requeue.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `recoverable_workday_rows(conn) -> list[dict]`
- Produces: `apply_requeue(conn, posting_ids) -> dict`
- CLI: `python3 scripts/requeue_workday_recoverable.py --db PATH --preview|--apply --json`

- [ ] **Step 1: Write failing safety tests**

Seed failed rows with exact reasons:

```python
COVERED_REASONS = (
    "resume upload zone never appeared",
    "apply button not found (posting closed?)",
)
```

Also seed a click-uncertain row, a submitted row, a stale row, a row with a confirmed attempt, and a row with a different failure reason. Assert only covered rows without finished click attempts appear in preview.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_workday_requeue.py`

Expected: FAIL because the command does not exist.

- [ ] **Step 3: Implement preview eligibility**

Require all of:

- Workday lane classification
- `status IN ('failed','manual')`
- exact covered reason prefix
- no application ledger row
- no `submission_attempts` row with `click_attempted=1`
- canonical posting not active or applied elsewhere

Return posting ID, company, title, tenant, reason, attempt count, and URL.

- [ ] **Step 4: Implement atomic compare-and-set apply**

Use `BEGIN IMMEDIATE`, recompute eligibility, and update only matching source state to `ready`, clearing `outcome` and setting `last_error='requeued after Workday entry repair'`. Preserve attempt count for audit.

- [ ] **Step 5: Add subprocess tests**

Verify preview does not change the DB hash. Verify apply changes only selected rows and is idempotent. Verify CLI output never includes Workday passwords or `wd_accounts` content.

- [ ] **Step 6: Document the exact command**

Add preview, backup, integrity, apply, and rollback instructions to README.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_workday_requeue.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/requeue_workday_recoverable.py tests/test_workday_requeue.py README.md
git commit -m "Requeue Workday entry failures safely"
```

### Task 5: Widen the Workday lane after guard validation

**Files:**
- Modify: `submission/lanes.py:20-27`
- Modify: `tests/test_submission_dispatcher.py`
- Modify: `test_throughput.py`

**Interfaces:**
- Changes: `WORKDAY.concurrency` from 1 to 2
- Changes: `WORKDAY.attempts_per_cycle` from 2 to 4
- Preserves: tenant exclusion from Task 1

- [ ] **Step 1: Write failing policy and observed-concurrency tests**

```python
def test_workday_policy_uses_two_workers_and_four_attempts():
    assert WORKDAY.concurrency == 2
    assert WORKDAY.attempts_per_cycle == 4
```

Update the existing thread test to seed four Workday rows across four tenants, block workers on a barrier, and assert maximum concurrent calls is exactly 2. Add two same-tenant rows and assert they are never concurrent.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py -k workday`

Expected: FAIL because Workday remains 1 and 2.

- [ ] **Step 3: Change only the Workday policy**

```python
WORKDAY = LanePolicy(
    "workday",
    frozenset({"workday"}),
    concurrency=2,
    attempts_per_cycle=4,
    automatic=True,
    preparable=True,
)
```

Do not alter Direct, Ashby, Oracle, or manual policies.

- [ ] **Step 4: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_submission_dispatcher.py tests/test_submission_lanes.py test_throughput.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add submission/lanes.py tests/test_submission_dispatcher.py test_throughput.py
git commit -m "Widen tenant-safe Workday lane"
```

### Task 6: Live Workday acceptance and rollout

**Files:**
- Modify: `docs/system-design.html`
- Test: all files from Tasks 1 through 5

**Interfaces:**
- Consumes: re-entry, failure classification, requeue CLI, and widened lane
- Produces: live dry-run and concurrency evidence

- [ ] **Step 1: Run the full suite and static checks**

Run:

```bash
python3 -m pytest -q
git diff --check
python3 scripts/requeue_workday_recoverable.py --db out/tracker.db --preview --json
```

Expected: tests pass and preview is nonmutating.

- [ ] **Step 2: Back up and integrity-check the live database**

Create `out/backups/pre-workday-repair-<UTC>.db`, chmod 0600, and run `PRAGMA integrity_check` on live and backup files.

- [ ] **Step 3: Dry-run representative failures**

Select at least one known upload-zone failure and one missing-Apply failure from different tenants. Run each through the real adapter with `dry_run=True`. Confirm recovery re-entry reaches the upload or submit boundary, and no click marker or attempt-ledger mutation occurs.

- [ ] **Step 4: Apply the covered-row requeue**

Run preview, inspect exact rows, then apply. Verify no click-uncertain, submitted, stale, or unrelated failure was changed.

- [ ] **Step 5: Observe one concurrency-1 control cycle**

Temporarily run `dispatch_cycle` with a copied policy using concurrency 1 and two different tenants. Record results and ensure no auth cross-talk.

- [ ] **Step 6: Observe the resident concurrency-2 cycle**

Reload the submit daemon from the stable checkout. Record worker IDs, tenant keys, start and finish times, and outcomes. Assert no overlapping same-tenant intervals and no more than two Workday workers.

- [ ] **Step 7: Compare outcomes**

Report attempts, confirmations, manual results, retryable failures, median duration, and upload-zone and Apply-button failure recurrence. If same-tenant overlap or uncertainty appears, pause Workday and revert only the policy commit.

- [ ] **Step 8: Update and commit system documentation**

```bash
git add docs/system-design.html
git commit -m "Document tenant-safe Workday recovery"
```

Run full pytest, SQLite integrity, service state checks, and `git status --short`. Confirm `out/tracker.db` remains unstaged.
