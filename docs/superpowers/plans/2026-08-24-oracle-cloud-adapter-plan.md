# Oracle Recruiting Cloud Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative Oracle Recruiting Cloud submission lane for the 37 current uniform Oracle Candidate Experience postings.

**Architecture:** Detection and identity use the verified Oracle Candidate Experience URL shape. A dedicated adapter handles only fixture-backed anonymous flows, returns unsupported tenant variants to manual before click, and uses the shared attempt, CAPTCHA, Q&A, and uncertainty safety contracts.

**Tech Stack:** Python 3.9, Playwright sync API, SQLite, HTMLParser, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-jobhunt-blocker-remediation-design.md`

## Global Constraints

- Classify only verified `*.oraclecloud.com/hcmUI/CandidateExperience/.../sites/.../job/<id>` URLs.
- Do not classify arbitrary Oracle pages as Oracle Recruiting Cloud.
- Start at concurrency 1 and 2 attempts per cycle.
- Support only fixture-backed anonymous or explicit account-gate states.
- CAPTCHA, unknown tenant variants, and unknown account states return manual before click.
- Mark the point of no return immediately before the final Submit click.
- Never retry a click-uncertain result.
- Preserve canonical posting dedupe and append-only application and attempt ledgers.
- Use TDD and frequent commits.
- Never stage `out/tracker.db`, screenshots, credentials, or captured personal data.
- Do not use Unicode em or en dashes in added prose.

## File structure

- Create `apply/oraclecloud_url.py`: URL parsing, canonical material, and site identity.
- Modify `apply/jd.py`: Oracle detection and public description extraction.
- Modify `submission/identity.py`: Oracle canonical key.
- Modify `submission/lanes.py`: isolated Oracle lane.
- Create `apply/oraclecloud.py`: fixture-backed Playwright adapter.
- Modify `submit_worker.py`: Oracle adapter routing.
- Create `tests/fixtures/oraclecloud/job-open.html`: sanitized public job page.
- Create `tests/fixtures/oraclecloud/job-closed.html`: explicit closed marker page.
- Create `tests/fixtures/oraclecloud/apply-anonymous.html`: sanitized anonymous form structure.
- Create `tests/test_oraclecloud.py`: parser, page state, fill, CAPTCHA, dry-run, and confirmation tests.
- Modify `tests/test_submission_lanes.py`, `tests/test_submission_identity.py`, and `test_submit.py`.
- Extend `submission/resolutions.py` and `scripts/retriage_resolved_postings.py`: re-triage newly detected technical-debt rows without requiring a wrapper mapping.

---

### Task 1: Oracle URL parsing, detection, identity, and lane

**Files:**
- Create: `apply/oraclecloud_url.py`
- Modify: `apply/jd.py:88-101, 158-172`
- Modify: `submission/identity.py:35-79`
- Modify: `submission/lanes.py:20-27`
- Modify: `tests/test_submission_lanes.py`
- Modify: `tests/test_submission_identity.py`
- Create: `tests/test_oraclecloud.py`

**Interfaces:**
- Produces: `OraclePostingUrl`
- Produces: `parse_oracle_posting_url(url: str) -> OraclePostingUrl | None`
- Produces: ATS string `oraclecloud`
- Produces: `ORACLE` lane with concurrency 1 and 2 attempts per cycle

- [ ] **Step 1: Write failing URL and lane tests**

```python
def test_parse_oracle_candidate_experience_url():
    parsed = parse_oracle_posting_url(
        "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992"
    )
    assert parsed.host == "egug.fa.us2.oraclecloud.com"
    assert parsed.locale == "en"
    assert parsed.site == "CX_1"
    assert parsed.job_id == "26011992"


def test_oracle_detection_is_narrow():
    assert detect_ats(ORACLE_JOB_URL) == "oraclecloud"
    assert detect_ats("https://www.oracle.com/careers") == "other"
    assert detect_ats("https://example.oraclecloud.com/not-a-job") == "other"


def test_oracle_lane_is_isolated():
    ats, lane = classify_url(ORACLE_JOB_URL)
    assert ats == "oraclecloud"
    assert lane.name == "oracle"
    assert lane.concurrency == 1
    assert lane.attempts_per_cycle == 2
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_oraclecloud.py tests/test_submission_lanes.py tests/test_submission_identity.py -k oracle`

Expected: FAIL because Oracle parsing and lane support do not exist.

- [ ] **Step 3: Implement strict URL parsing**

```python
_ORACLE_PATH_RE = re.compile(
    r"^/hcmUI/CandidateExperience/(?P<locale>[^/]+)/sites/(?P<site>[^/]+)/job/(?P<job_id>\d+)/?$",
    re.I,
)

@dataclass(frozen=True)
class OraclePostingUrl:
    host: str
    locale: str
    site: str
    job_id: str


def parse_oracle_posting_url(url: str) -> OraclePostingUrl | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    match = _ORACLE_PATH_RE.fullmatch(parsed.path)
    if parsed.scheme != "https" or not host.endswith(".oraclecloud.com") or not match:
        return None
    return OraclePostingUrl(host, match["locale"], match["site"], match["job_id"])
```

- [ ] **Step 4: Add narrow detection and canonical identity**

`detect_ats` returns `oraclecloud` only when the parser succeeds. Identity material is:

```python
f"oraclecloud:{parsed.host}:{parsed.site.lower()}:{parsed.job_id}"
```

Tracking queries and locale casing must not alter the key.

- [ ] **Step 5: Add the isolated lane**

```python
ORACLE = LanePolicy(
    "oracle",
    frozenset({"oraclecloud"}),
    concurrency=1,
    attempts_per_cycle=2,
    automatic=True,
    preparable=True,
)
```

Add it to `POLICIES` without changing other lane budgets.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_oraclecloud.py tests/test_submission_lanes.py tests/test_submission_identity.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add apply/oraclecloud_url.py apply/jd.py submission/identity.py submission/lanes.py tests/test_oraclecloud.py tests/test_submission_lanes.py tests/test_submission_identity.py
git commit -m "Classify Oracle Recruiting Cloud postings"
```

### Task 2: Public job description and sanitized fixtures

**Files:**
- Modify: `apply/jd.py`
- Create: `tests/fixtures/oraclecloud/job-open.html`
- Create: `tests/fixtures/oraclecloud/job-closed.html`
- Create: `tests/fixtures/oraclecloud/apply-anonymous.html`
- Modify: `tests/test_oraclecloud.py`

**Interfaces:**
- Produces: `_oraclecloud(url: str) -> str`
- Produces: `oracle_closed_marker(body_text: str) -> str | None`
- Fixtures contain no candidate data, credentials, cookies, or headers

- [ ] **Step 1: Capture sanitized public fixtures**

Fetch the public American Express sample job `26011992` without logging in. Keep only:

- `<base>` attributes needed for host and site parsing
- `og:title` and `og:description`
- open or closed marker copy
- representative Apply button markup
- representative anonymous form controls with synthetic labels and values

Replace company-specific prose with `Example Company` except for structural selectors. Verify fixture secret scan returns no email, phone, cookie, token, or candidate answer.

- [ ] **Step 2: Write failing JD and page-state tests**

```python
def test_oracle_jd_uses_public_og_description(monkeypatch, fixture_text):
    monkeypatch.setattr(jd, "_get", lambda _: fixture_text("job-open.html"))
    assert jd._oraclecloud(ORACLE_JOB_URL) == "Example job description"


def test_oracle_closed_marker_is_explicit(fixture_text):
    assert oracle_closed_marker(fixture_text("job-closed.html")) == "job is no longer available"
    assert oracle_closed_marker(fixture_text("job-open.html")) is None
```

- [ ] **Step 3: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_oraclecloud.py -k 'jd or closed or fixture'`

Expected: FAIL because Oracle JD and page-state helpers do not exist.

- [ ] **Step 4: Implement metadata extraction with HTMLParser**

Create a small parser that reads `meta[property='og:description']`. Unescape HTML entities, normalize whitespace, and cap the result at 12,000 characters. `fetch_jd` routes `oraclecloud` to this helper and falls back to existing generic HTML stripping on failure.

- [ ] **Step 5: Implement explicit closure markers**

Use a fixed marker tuple:

```python
ORACLE_CLOSED_MARKERS = (
    "job is no longer available",
    "position is no longer available",
    "job posting has been removed",
    "job not found",
)
```

Do not infer closure from a missing Apply control alone.

- [ ] **Step 6: Run fixture and full tests**

Run: `python3 -m pytest -q tests/test_oraclecloud.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS and fixtures pass the secret scan.

- [ ] **Step 7: Commit**

```bash
git add apply/jd.py tests/fixtures/oraclecloud tests/test_oraclecloud.py
git commit -m "Read Oracle public job metadata"
```

### Task 3: Oracle pre-submit adapter flow

**Files:**
- Create: `apply/oraclecloud.py`
- Modify: `tests/test_oraclecloud.py`

**Interfaces:**
- Produces: `apply_oraclecloud(url, resume_pdf, slug, dry_run=True) -> dict`
- Consumes: shared `qa.EXTRACT_JS`, `qa.get_answers`, `qa.fill_answers`, `safe_screenshot`, and `configure_page`

- [ ] **Step 1: Write failing anonymous-flow tests**

Build a fake page from the sanitized form fixture. Assert the adapter:

1. navigates to the exact canonical job URL
2. detects closure before Apply
3. clicks an exact visible Apply control
4. detects explicit account-only gates as manual
5. uploads the PDF to a resume-labelled file input
6. fills name, email, phone, and approved links
7. runs up to three shared Q&A passes
8. returns manual for required unanswered fields
9. returns dry-run success at the submit boundary without clicking

Example assertion:

```python
def test_oracle_dry_run_reaches_submit_boundary_without_click(fake_oracle_page, pdf):
    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-dry", dry_run=True)
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert result["unanswered"] == []
    assert fake_oracle_page.submit_clicks == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_oraclecloud.py -k 'apply or dry_run or account_gate or required'`

Expected: FAIL because `apply.oraclecloud` does not exist.

- [ ] **Step 3: Implement liveness and Apply entry**

Launch the existing local stealth context, configure the page, navigate with a 45-second timeout, and inspect explicit closed markers. Use exact visible candidates:

```python
APPLY_SELECTORS = (
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    "[data-bind*='apply'][role='button']",
)
```

If none are visible and no closure marker exists, return `outcome='manual'`, reason `unsupported Oracle tenant variant: apply control not found`, and no click.

- [ ] **Step 4: Implement pre-submit fill**

Select the resume input by label or nearby text, never `.first` without context. Fill core controls after resume parsing. Use the shared Q&A engine for custom fields, then re-extract required controls. Any required empty control returns manual with the first six labels.

- [ ] **Step 5: Add account and CAPTCHA fail-closed gates**

Before final submit, detect visible sign-in-only, account-required, hCaptcha, reCAPTCHA, or DataDome UI. Return manual with `retryable=False`, `click_attempted=False`, and `submission_uncertain=False`.

- [ ] **Step 6: Record a local filled screenshot**

Use `safe_screenshot`. Put only the local path into `result['artifact_refs']`. Do not print or send it.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_oraclecloud.py tests/test_qa_policy.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apply/oraclecloud.py tests/test_oraclecloud.py
git commit -m "Prepare Oracle Recruiting applications"
```

### Task 4: Oracle submit point, confirmation, and worker routing

**Files:**
- Modify: `apply/oraclecloud.py`
- Modify: `submit_worker.py:20-55`
- Modify: `tests/test_oraclecloud.py`
- Modify: `tests/test_submission_lanes.py`
- Modify: `test_submit.py`

**Interfaces:**
- Extends: `apply_oraclecloud` with live final-submit behavior
- Extends: `submit_worker._adapter` for `oraclecloud`
- Uses: `mark_submit_attempted`, `confirmation_observed`, and `mark_unconfirmed`

- [ ] **Step 1: Write failing submit-safety tests**

```python
def test_oracle_marks_point_of_no_return_immediately_before_click(fake_page):
    result = apply_oraclecloud(ORACLE_JOB_URL, PDF, "oracle-live", dry_run=False)
    assert fake_page.events[-2:] == ["mark_submit_attempted", "submit_click"]
    assert result["submitted"] is True


def test_oracle_unconfirmed_click_is_manual_and_not_retryable(fake_page):
    fake_page.confirmation = False
    result = apply_oraclecloud(ORACLE_JOB_URL, PDF, "oracle-live", dry_run=False)
    assert result["submission_uncertain"] is True
    assert result["click_attempted"] is True
    assert result["retryable"] is False
```

Add a worker routing test that `_adapter('other', ORACLE_JOB_URL)` returns `apply_oraclecloud`, `False`, `oraclecloud`, and the canonical target URL.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_oraclecloud.py tests/test_submission_lanes.py test_submit.py -k oracle`

Expected: FAIL because live submit and worker routing are incomplete.

- [ ] **Step 3: Implement the final click boundary**

Locate exact visible Submit candidates, call `mark_submit_attempted()`, and click immediately. No network, screenshot, or logging call may occur between marker and click.

- [ ] **Step 4: Implement confirmation and uncertainty**

Wait a bounded five seconds, then evaluate body and URL with `confirmation_observed`. On confirmation, return `ok=True`, `submitted=True`, reason `confirmed`. Otherwise call `mark_unconfirmed`.

- [ ] **Step 5: Route the adapter**

Add:

```python
if detected == "oraclecloud":
    from oraclecloud import apply_oraclecloud
    return apply_oraclecloud, False, detected, target_url
```

Keep unsupported and all other branches unchanged.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_oraclecloud.py tests/test_submission_lanes.py test_submit.py tests/test_submission_executor.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add apply/oraclecloud.py submit_worker.py tests/test_oraclecloud.py tests/test_submission_lanes.py test_submit.py
git commit -m "Submit Oracle Recruiting applications safely"
```

### Task 5: Re-triage existing Oracle technical debt

**Files:**
- Modify: `submission/resolutions.py`
- Modify: `scripts/retriage_resolved_postings.py`
- Modify: `tests/test_resolution_retriage.py`

**Interfaces:**
- Extends: `retriage_candidates(conn, ats: str | None = None)`
- Allows current-URL technical debt when new detection maps the URL to an automatic supported lane

- [ ] **Step 1: Write failing Oracle re-triage tests**

Seed a manual Oracle URL with `last_error='no adapter for other'`, no resolution mapping, and no finished attempt. Assert preview with `ats='oraclecloud'` includes it and apply moves it to `queued`.

Also seed click-uncertain, applied-alias, stale, and finished-attempt Oracle rows. Assert they remain unchanged.

- [ ] **Step 2: Run test and verify RED**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py -k oracle`

Expected: FAIL because re-triage requires a resolution mapping.

- [ ] **Step 3: Generalize technical-debt detection safely**

When current `detect_ats(url)` returns a supported automatic lane, allow exact technical reasons `no adapter for other` and `no adapter for oraclecloud` without a mapping. Keep every existing ledger, canonical, state, and finished-attempt exclusion.

- [ ] **Step 4: Add CLI ATS filter**

Add `--ats oraclecloud`. JSON output includes only safe fields. Preview remains default and nonmutating.

- [ ] **Step 5: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py tests/test_oraclecloud.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add submission/resolutions.py scripts/retriage_resolved_postings.py tests/test_resolution_retriage.py
git commit -m "Queue detected Oracle technical debt"
```

### Task 6: Oracle dry-run and live canary rollout

**Files:**
- Modify: `README.md`
- Modify: `docs/system-design.html`
- Test: all files from Tasks 1 through 5

**Interfaces:**
- Consumes: Oracle detector, lane, adapter, worker routing, and re-triage
- Produces: one reviewed dry run and one live canary result

- [ ] **Step 1: Document scope and rollback**

Document supported URL shape, anonymous-flow limit, manual unsupported variants, lane budget, dry-run command, re-triage preview, canary, and pause procedure.

- [ ] **Step 2: Run full static and fixture verification**

Run:

```bash
python3 -m pytest -q
git diff --check
python3 scripts/retriage_resolved_postings.py --db out/tracker.db --preview --ats oraclecloud --json
```

Expected: tests pass and preview is nonmutating.

- [ ] **Step 3: Back up and integrity-check SQLite**

Create `out/backups/pre-oracle-canary-<UTC>.db`, chmod 0600, and verify integrity on live and backup files.

- [ ] **Step 4: Run representative real-form dry runs**

Use one American Express, one JPMorgan, and one other Oracle tenant. Require each to produce one of:

- dry-run submit boundary with zero missing fields
- explicit account gate manual
- explicit CAPTCHA manual
- explicit unsupported tenant variant manual
- explicit stale marker

No dry run may create an attempt row or click marker.

- [ ] **Step 5: Apply Oracle re-triage**

Inspect the exact preview, then apply. Verify zero duplicate, applied-alias, offseason, click-uncertain, or finished-attempt leaks.

- [ ] **Step 6: Run one live canary**

Choose one unattempted, quality-passing, duplicate-free Oracle posting whose dry run reached the submit boundary. Run exactly one live dispatcher execution. Record attempt ID, click state, confirmation, duration, tenant, and artifact availability.

- [ ] **Step 7: Decide lane continuation from evidence**

- Confirmed: leave Oracle enabled at concurrency 1 and attempts per cycle 2.
- Manual before click: fix only the observed fixture-backed issue, then repeat dry run before another canary.
- Click uncertain: pause Oracle immediately and never retry that posting.
- CAPTCHA or unsupported variant: keep that row manual and continue only with a different fixture-backed tenant.

- [ ] **Step 8: Update docs and final verification**

```bash
git add README.md docs/system-design.html
git commit -m "Document Oracle Recruiting lane"
```

Run full pytest, SQLite integrity, lane queue metrics, Sheet readback, service state, `git diff --check`, and `git status --short`. Confirm `out/tracker.db` and screenshots remain unstaged.
