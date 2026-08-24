# Manual Handoff and Submission Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make nonautomatic forms visible and actionable without CAPTCHA solvers, fix confirmation and uncertainty classification, and expose one safe daily Telegram and Sheet handoff.

**Architecture:** Lane policies explicitly distinguish resume preparation from automatic submission. Pre-submit CAPTCHA checks stop before the point of no return, attempt artifacts and missing fields remain local, and digest and Sheet views derive manual actions from authoritative posting and attempt state.

**Tech Stack:** Python 3.9, SQLite, Playwright sync API, Google Sheets API, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-jobhunt-blocker-remediation-design.md`

## Global Constraints

- Never solve or bypass CAPTCHAs.
- Never retry a click-uncertain posting.
- Never put local paths, cookies, headers, credentials, or form answers into Telegram or Google Sheets.
- Preserve one routine daily Telegram copy and no per-posting notification pile.
- Keep the Ashby automatic lane disabled and do not modify macOS auto-login, power, or caffeinate settings.
- Preserve canonical duplicate checks and append-only application and attempt ledgers.
- Use TDD with a failing test before every behavior change.
- Stage and commit only named source and test files. Never stage `out/tracker.db`.
- Do not use Unicode em or en dashes in added prose.

## File structure

- Modify `submission/lanes.py`: explicit preparation policy and nonautomatic-ready reconciliation.
- Modify `drip.py`: route a quality-checked resume to `ready` or `manual` from the lane policy.
- Modify `apply/submission_state.py`: confirmation URL detection.
- Create `apply/artifacts.py`: sanitized best-effort local screenshots.
- Modify `apply/lever.py`: CAPTCHA preflight and artifact result.
- Modify `apply/smartrecruiters.py`: expanded DataDome preflight and artifact result.
- Modify `submission/attempts.py`: structured missing-field persistence.
- Modify `submission/executor.py`: propagate missing fields and artifact references.
- Modify `digest.py`: verification, manual completion, and engineering-debt sections.
- Modify `sheet_tracker.py`: `Manual Actions` tab and safe fields.
- Modify focused tests under `tests/` and `test_throughput.py`.

---

### Task 1: Explicit preparation policy and manual-lane reconciliation

**Files:**
- Modify: `submission/lanes.py:11-79`
- Modify: `drip.py:265-309`
- Modify: `tests/test_submission_lanes.py`
- Modify: `test_throughput.py`

**Interfaces:**
- Produces: `LanePolicy.preparable: bool`
- Produces: `preparation_destination(conn, url) -> tuple[str, str | None]`
- Produces: `reconcile_nonautomatic_ready(conn) -> int`
- Consumes: `classify_url(url)` and `ashby_enabled(conn)`

- [ ] **Step 1: Write failing lane-policy tests**

```python
def test_manual_lane_is_preparable_but_not_automatic():
    ats, lane = classify_url("https://jobs.smartrecruiters.com/acme/1")
    assert ats == "smartrecruiters"
    assert lane.preparable is True
    assert lane.automatic is False


def test_reconcile_nonautomatic_ready_moves_smartrecruiters_to_manual(conn):
    conn.execute(
        "INSERT INTO postings(posting_id,status,url) VALUES (?,?,?)",
        ("sr-1", "ready", "https://jobs.smartrecruiters.com/acme/1"),
    )
    conn.commit()
    assert reconcile_nonautomatic_ready(conn) == 1
    assert conn.execute(
        "SELECT status,outcome,last_error FROM postings WHERE posting_id='sr-1'"
    ).fetchone() == (
        "manual",
        "manual",
        "prepared for manual completion: smartrecruiters",
    )
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_submission_lanes.py -k 'preparable or reconcile_nonautomatic'`

Expected: FAIL because `LanePolicy.preparable` and `reconcile_nonautomatic_ready` do not exist.

- [ ] **Step 3: Add the minimal preparation interfaces**

```python
@dataclass(frozen=True)
class LanePolicy:
    name: str
    ats: frozenset[str]
    concurrency: int
    attempts_per_cycle: int
    automatic: bool
    preparable: bool

DIRECT = LanePolicy("direct", frozenset({"greenhouse", "lever", "workable", "rippling"}), 2, 8, True, True)
WORKDAY = LanePolicy("workday", frozenset({"workday"}), 1, 2, True, True)
ASHBY = LanePolicy("ashby", frozenset({"ashby"}), 1, 1, False, True)
MANUAL = LanePolicy("manual", frozenset({"smartrecruiters"}), 0, 0, False, True)
EMAIL = LanePolicy("email", frozenset({"email"}), 0, 0, False, False)
WAAS = LanePolicy("waas", frozenset({"waas"}), 0, 0, False, False)
UNSUPPORTED = LanePolicy("unsupported", frozenset({"other", "icims"}), 0, 0, False, False)


def preparation_destination(conn: sqlite3.Connection, url: str) -> tuple[str, str | None]:
    ats, lane = classify_url(url)
    if lane.automatic or (lane.name == ASHBY.name and ashby_enabled(conn)):
        return "ready", None
    if lane.preparable:
        return "manual", f"prepared for manual completion: {ats}"
    return "manual", f"no adapter for {ats}"
```

Implement `reconcile_nonautomatic_ready` with a compare-and-set update for only `status='ready'`, commit once, and return the changed row count.

- [ ] **Step 4: Route tailored rows through the preparation destination**

In `drip.tailor_one`, compute the destination after the resume passes quality checks. Store the email artifact first, then use `transition_claim(..., destination, commit=False)`. For `manual`, also set `outcome='manual'` and the preparation reason before committing.

- [ ] **Step 5: Add a nine-row mixed-lane drain regression**

Add to `test_throughput.py` a test with Greenhouse and SmartRecruiters rows. Assert Greenhouse ends `ready`, SmartRecruiters ends `manual`, and every row is processed exactly once.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_submission_lanes.py test_throughput.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add submission/lanes.py drip.py tests/test_submission_lanes.py test_throughput.py
git commit -m "Route prepared manual ATS rows visibly"
```

### Task 2: Confirmation and pre-submit CAPTCHA correctness

**Files:**
- Modify: `apply/submission_state.py:18-28`
- Modify: `apply/lever.py:24-180`
- Modify: `apply/smartrecruiters.py:26-101`
- Modify: `tests/test_lever.py`
- Modify: `tests/test_qa_policy.py`
- Modify: `test_submit.py`

**Interfaces:**
- Produces: `confirmation_observed(body_text, url)` recognizing a terminal `/thanks` path segment
- Produces: `lever_captcha_present(page) -> bool`
- Extends: `_preflight_outcome(body_text, captcha_present)` for DataDome text markers

- [ ] **Step 1: Write failing confirmation and CAPTCHA tests**

```python
def test_lever_thanks_path_is_confirmation():
    assert confirmation_observed("", "https://jobs.lever.co/acme/id/thanks")
    assert not confirmation_observed("", "https://example.com/thanksgiving")


def test_lever_hcaptcha_stops_before_submit(fake_page):
    fake_page.add_visible_iframe("https://newassets.hcaptcha.com/captcha/v1/abc")
    result = apply_lever(URL, PDF, "lever-captcha", dry_run=False)
    assert result["outcome"] == "manual"
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert fake_page.submit_clicks == 0
```

Add a SmartRecruiters unit test where body text contains `datadome` without an iframe. Expect a definitive manual CAPTCHA outcome.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_lever.py tests/test_qa_policy.py test_submit.py -k 'thanks or captcha or datadome'`

Expected: FAIL because `/thanks` and Lever preflight are not supported.

- [ ] **Step 3: Tighten confirmation URL matching**

Use a path-bounded pattern:

```python
_CONFIRMATION_URL_RE = re.compile(
    r"(?:confirmation|thank[-_]?you|application[-_]?submitted|(?:^|/)thanks(?:/|$))",
    re.IGNORECASE,
)
```

Apply it to the parsed URL path plus existing full-URL compatibility text so query values cannot create a false confirmation.

- [ ] **Step 4: Add Lever CAPTCHA preflight before `mark_submit_attempted`**

```python
def lever_captcha_present(page) -> bool:
    selectors = (
        "iframe[src*='hcaptcha.com']",
        "iframe[title*='hcaptcha' i]",
        "textarea[name='h-captcha-response']",
        "[data-sitekey][class*='h-captcha']",
    )
    return any(page.locator(selector).count() > 0 for selector in selectors)
```

When present, return `outcome='manual'`, `retryable=False`, `click_attempted=False`, `submission_uncertain=False`, and reason `Lever hCaptcha requires manual completion`.

- [ ] **Step 5: Expand SmartRecruiters DataDome preflight**

Treat a visible DataDome iframe or body markers `datadome`, `captcha-delivery.com`, or `verify you are human` as manual CAPTCHA before the click.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_lever.py tests/test_qa_policy.py test_submit.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS and no submit marker is created in preflight tests.

- [ ] **Step 7: Commit**

```bash
git add apply/submission_state.py apply/lever.py apply/smartrecruiters.py tests/test_lever.py tests/test_qa_policy.py test_submit.py
git commit -m "Classify ATS confirmations and CAPTCHAs safely"
```

### Task 3: Persist local artifacts and structured missing fields

**Files:**
- Create: `apply/artifacts.py`
- Modify: `apply/lever.py`
- Modify: `apply/smartrecruiters.py`
- Modify: `submission/attempts.py:5-66`
- Modify: `submission/executor.py:68-80, 230-257`
- Modify: `tests/test_submission_attempts.py`
- Modify: `tests/test_submission_executor.py`
- Create: `tests/test_apply_artifacts.py`

**Interfaces:**
- Produces: `safe_screenshot(page, slug, stage, root=SHOTS) -> str | None`
- Extends: `finish_attempt(..., unanswered=None)`
- Stores: `submission_attempts.unanswered_json TEXT NOT NULL DEFAULT '[]'`

- [ ] **Step 1: Write failing artifact and ledger tests**

```python
def test_safe_screenshot_returns_local_path_and_sanitizes_stage(tmp_path):
    page = FakePage()
    path = safe_screenshot(page, "Acme role", "filled\n../../bad", root=tmp_path)
    assert Path(path).parent == tmp_path
    assert ".." not in Path(path).name
    assert page.paths == [path]


def test_finish_attempt_persists_unanswered(conn):
    finish_attempt(
        conn,
        attempt_id="a1",
        outcome="manual",
        reason_code="manual",
        raw_reason="needs location",
        click_attempted=False,
        confirmation_observed=False,
        artifact_refs={"filled_form_screenshot": "/local/shot.png"},
        unanswered=["Current location"],
    )
    row = conn.execute(
        "SELECT artifact_refs_json,unanswered_json FROM submission_attempts WHERE attempt_id='a1'"
    ).fetchone()
    assert json.loads(row[1]) == ["Current location"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_apply_artifacts.py tests/test_submission_attempts.py tests/test_submission_executor.py`

Expected: FAIL because the helper and `unanswered_json` do not exist.

- [ ] **Step 3: Add best-effort screenshot helper**

```python
def safe_screenshot(page, slug: str, stage: str, *, root: Path) -> str | None:
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{slug}_{stage}").strip("._")
    path = root / f"{safe[:160]}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=1000)
        return str(path)
    except Exception:
        return None
```

Do not log page content or secrets.

- [ ] **Step 4: Add idempotent attempt schema migration**

Extend `SCHEMA` with `unanswered_json`. In `ensure_submission_attempts`, inspect `PRAGMA table_info(submission_attempts)` and execute:

```python
if "unanswered_json" not in columns:
    conn.execute("ALTER TABLE submission_attempts ADD COLUMN unanswered_json TEXT NOT NULL DEFAULT '[]'")
```

Extend `finish_attempt` to JSON-encode `unanswered or []`.

- [ ] **Step 5: Propagate adapter results through the executor**

Pass `result.get("unanswered") or []` from `_finish_attempt_once` into `finish_attempt`. Ensure the executor's returned result retains `artifact_refs` and `unanswered` without adding local paths to notices.

- [ ] **Step 6: Use the helper in Lever and SmartRecruiters**

Set:

```python
shot = safe_screenshot(page, slug, "filled", root=SHOTS)
if shot:
    result.setdefault("artifact_refs", {})["filled_form_screenshot"] = shot
```

Do this before each manual return and after a filled dry run. Do not attach the screenshot to Telegram or Sheet data.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_apply_artifacts.py tests/test_submission_attempts.py tests/test_submission_executor.py tests/test_lever.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apply/artifacts.py apply/lever.py apply/smartrecruiters.py submission/attempts.py submission/executor.py tests/test_apply_artifacts.py tests/test_submission_attempts.py tests/test_submission_executor.py
git commit -m "Record manual handoff artifacts safely"
```

### Task 4: Correct digest classification and Telegram copy

**Files:**
- Modify: `digest.py:72-289`
- Modify: `tests/test_digest_phone.py`

**Interfaces:**
- Extends: `collect(conn, since)` with `manual_finish` and corrected `verify`
- Preserves: `_send_phone_copy(d)` as the only routine Telegram delivery

- [ ] **Step 1: Write failing digest classification tests**

Seed three rows:

```python
uncertain = {"status": "manual", "last_error": "submit clicked but confirmation was not observed; verify possible prior submission"}
captcha = {"status": "manual", "last_error": "Lever hCaptcha requires manual completion"}
debt = {"status": "manual", "last_error": "no adapter for other"}
```

Add a finished attempt for the uncertain row with `click_attempted=1` and `confirmation_observed=0`. Assert:

```python
assert [row[0] for row in data["verify"]] == ["Uncertain Co"]
assert [row[0] for row in data["manual_finish"]] == ["Captcha Co"]
assert data["manual_debt"]["no adapter for"] == 1
assert not data["manual_ask"]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python3 -m pytest -q tests/test_digest_phone.py -k 'verify or manual_finish'`

Expected: FAIL because the verify query uses `outcome='submitted'` and `manual_finish` does not exist.

- [ ] **Step 3: Query verification from attempt truth**

Use an `EXISTS` subquery against `submission_attempts` where `click_attempted=1` and `confirmation_observed=0`. Exclude those posting IDs from manual input and manual completion lists.

Classify these reason prefixes as manual completion:

```python
MANUAL_FINISH_PREFIXES = (
    "needs manual Lever location selection",
    "Lever hCaptcha requires manual completion",
    "SmartRecruiters CAPTCHA requires manual completion",
    "Ashby rejected the submission as possible spam",
    "ashby automation disabled after spam rejection",
    "prepared for manual completion:",
)
```

- [ ] **Step 4: Add compact full and phone sections**

The full digest prints all newly seen manual completion rows with company, role, exact action, and URL. `compose_short` prints counts, the first deadline-sensitive items that fit, and the fixed Sheet URL. Keep the existing `SHORT_LIMIT` assertion.

- [ ] **Step 5: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_digest_phone.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS and `compose_short` stays under the configured limit.

- [ ] **Step 6: Commit**

```bash
git add digest.py tests/test_digest_phone.py
git commit -m "Separate verification from manual completion"
```

### Task 5: Add a complete Manual Actions Sheet tab

**Files:**
- Modify: `sheet_tracker.py:76-246`
- Modify: `tests/test_sheet_tracker.py`

**Interfaces:**
- Produces: `_collect_manual_actions(conn) -> list[list]`
- Produces: `_ensure_sheet(sid, title) -> int`
- Preserves: existing Dashboard, Applications, and Pipeline output

- [ ] **Step 1: Write failing manual-action collection tests**

Create a temporary tracker database with CAPTCHA, click-uncertain, technical-debt, and submitted rows. Assert the output columns are:

```python
[
    "Company", "Role", "ATS", "Action", "URL", "Age", "Attempt state",
    "Prepared resume", "Prepared screenshot", "Latest reason"
]
```

Assert submitted rows and pure engineering-debt rows are absent. Assert local artifact paths and raw answers are absent.

- [ ] **Step 2: Run test and verify RED**

Run: `python3 -m pytest -q tests/test_sheet_tracker.py -k manual`

Expected: FAIL because `_collect_manual_actions` does not exist.

- [ ] **Step 3: Implement safe collection**

Join manual postings to `emails` and the latest `submission_attempts` row. Derive booleans for prepared resume and screenshot. Emit only booleans, never local paths or JSON contents.

- [ ] **Step 4: Ensure the tab exists for old and new spreadsheets**

Add `Manual Actions` to new-sheet creation. For an existing spreadsheet, call batchUpdate `addSheet` only when `_sheet_ids` lacks the title. Make the operation idempotent.

- [ ] **Step 5: Write and format the tab**

Clear and update `Manual Actions!A:J`, freeze the header, apply wrap and filter, and color the Attempt state and Action columns. Do not change the existing three tabs.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_sheet_tracker.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add sheet_tracker.py tests/test_sheet_tracker.py
git commit -m "Add manual actions to jobhunt tracker"
```

### Task 6: Integrate and validate Phase 1 without sending notifications

**Files:**
- Modify: `README.md`
- Modify: `docs/system-design.html`
- Test: all files from Tasks 1 through 5

**Interfaces:**
- Consumes: all Phase 1 interfaces
- Produces: reviewed dry-run output and rollout evidence

- [ ] **Step 1: Update architecture documentation**

Document preparable-manual semantics, CAPTCHA preflight, uncertainty verification, local-only artifact paths, the Manual Actions tab, and the one-digest rule.

- [ ] **Step 2: Run the full verification suite**

Run:

```bash
python3 -m pytest -q
python3 digest.py --dry-run
python3 - <<'PY'
import sqlite3
from submission.lanes import classify_url
con = sqlite3.connect('file:out/tracker.db?mode=ro', uri=True)
rows = con.execute("SELECT url FROM postings WHERE status='ready'").fetchall()
print(sum(not classify_url(url)[1].automatic for (url,) in rows))
PY
```

Expected: pytest passes, digest prints but does not send, and nonautomatic ready count is reported before live reconciliation.

- [ ] **Step 3: Back up and reconcile live state**

Create a timestamped SQLite `.backup`, verify `PRAGMA integrity_check`, run the new reconciliation once, and verify nonautomatic ready count is zero. Do not send Telegram during this step.

- [ ] **Step 4: Run Sheet sync and read back row counts**

Run `python3 sheet_tracker.py`, then read the Sheets API response to verify `Manual Actions` row count equals the safe live query count.

- [ ] **Step 5: Review and commit docs**

```bash
git add README.md docs/system-design.html
git commit -m "Document manual ATS handoff workflow"
```

- [ ] **Step 6: Final safety checks**

Run `git diff --check`, `git status --short`, full pytest, SQLite integrity, and a secret/path scan of Sheet and Telegram payload fixtures. Confirm `out/tracker.db` is not staged.
