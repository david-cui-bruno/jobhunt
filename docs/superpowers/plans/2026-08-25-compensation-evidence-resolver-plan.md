# Compensation Evidence Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve required numeric compensation questions from fresh job-specific public evidence, with retained provenance and fail-closed browser behavior.

**Architecture:** A new `compensation` package separates schema/models, conservative extraction/normalization, provider-backed research, and cache-only resolution. Hourly background work researches compensation-blocked postings and stores evidence; Playwright adapters never call a search provider and can only consume a fresh exact cache match. Existing preview-first retriage remains the only path back to `ready`.

**Tech Stack:** Python 3.9 standard library (`dataclasses`, `decimal`, `json`, `statistics`, `urllib`, `sqlite3`), Tavily Search API over HTTPS, pytest, SQLite

**Spec:** `docs/superpowers/specs/2026-08-25-jobhunt-approved-answers-compensation-design.md`

## Global Constraints

- Employer-published USD range for the exact posting wins and resolves to its midpoint.
- Otherwise require at least two independent approved domains with compatible role, location, employment type, currency, and period.
- Allowed market domains are `levels.fyi`, `glassdoor.com`, `indeed.com`, `ziprecruiter.com`, and `salary.com`; employer evidence must come from the canonical fetched posting.
- Evidence is valid for 30 days.
- Hourly values round to the nearest whole dollar; annual values round to the nearest $1,000.
- Convert annual/hourly using 2,080 hours only when the requested period is explicit.
- Reject non-USD, missing URLs, duplicate domains, ambiguous periods, stale evidence, role/location mismatch, and spreads where maximum exceeds twice minimum.
- Search queries contain only company, title/role family, location, employment type, currency, and period. Never include candidate PII.
- Missing key/provider failure/malformed content stores no resolution and leaves the posting manual.
- Evidence creation never changes posting status, submission flags, attempts, or applications.
- Do not release Oracle in this rollout.
- Use TDD and commit each independently reviewed task.

## File Structure

- Create `compensation/__init__.py`: stable public exports.
- Create `compensation/models.py`: immutable context, observation, and resolution dataclasses.
- Create `compensation/schema.py`: `compensation_evidence` schema and cache persistence.
- Create `compensation/normalize.py`: conservative USD range extraction and unit conversion.
- Create `compensation/resolve.py`: context compatibility, evidence hierarchy, median, and cache lookup.
- Create `compensation/research.py`: search-provider protocol, Tavily adapter, posting research orchestration.
- Create `scripts/prepare_compensation.py`: explicit one-or-many posting CLI.
- Modify `submission/database.py`: initialize compensation schema.
- Modify `submission/executor.py`: pass public posting context to the child worker.
- Modify `submit_worker.py`: expose posting context before importing adapters/QA.
- Modify `apply/qa.py`: cache-only compensation answer rendering and exact approval.
- Modify `drip.py`: bounded periodic research for existing compensation blockers.
- Modify `submission/resolutions.py`: compensation-aware preview/apply candidate support.
- Modify `scripts/retriage_resolved_postings.py`: merge compensation candidates into existing preview-first CLI.
- Create `tests/test_compensation.py`: models, normalization, resolution, and schema.
- Create `tests/test_compensation_cli.py`: CLI and fake-provider integration.
- Modify `tests/test_qa_policy.py`: cache-only QA behavior.
- Modify `tests/test_resolution_retriage.py`: preview/apply and CAS safety.
- Modify `test_throughput.py`: hourly research remains bounded and provider-disabled without a key.

---

### Task 1: Define immutable models and SQLite evidence storage

**Files:**
- Create: `compensation/__init__.py`
- Create: `compensation/models.py`
- Create: `compensation/schema.py`
- Modify: `submission/database.py:1-20`
- Create: `tests/test_compensation.py`

**Interfaces:**
- Produces: `JobContext`, `EvidenceObservation`, `CompensationResolution` dataclasses.
- Produces: `ensure_compensation_schema(conn)`, `store_resolution(conn, resolution)`, `load_resolution(conn, context, now) -> CompensationResolution | None`.

- [ ] **Step 1: Write failing model and schema tests**

```python
from compensation.models import JobContext, EvidenceObservation, CompensationResolution
from compensation.schema import ensure_compensation_schema, load_resolution, store_resolution


def context() -> JobContext:
    return JobContext(
        posting_id="p1", company="Acme", title="Software Engineer Intern",
        location="New York, NY", employment_type="intern", currency="USD", period="hour",
    )


def test_schema_round_trip_requires_exact_fresh_context(tmp_path):
    conn = sqlite3.connect(tmp_path / "tracker.db")
    conn.row_factory = sqlite3.Row
    ensure_compensation_schema(conn)
    observation = EvidenceObservation(
        url="https://levels.fyi/acme", domain="levels.fyi", title="Acme intern pay",
        low=Decimal("40"), high=Decimal("50"), point=None,
        currency="USD", period="hour", source_kind="market", observed_at=1_787_600_000,
    )
    resolution = CompensationResolution(
        context=context(), amount=Decimal("45"), method="market_median",
        evidence=(observation,), researched_at=1_787_600_000, expires_at=1_790_192_000,
    )
    store_resolution(conn, resolution)
    assert load_resolution(conn, context(), now=1_787_600_001) == resolution
    wrong = dataclasses.replace(context(), company="Other")
    assert load_resolution(conn, wrong, now=1_787_600_001) is None
    assert load_resolution(conn, context(), now=1_790_192_001) is None
```

Add a test that `connect_tracker(tmp_path / "tracker.db")` creates the new table without committing an existing caller transaction.

- [ ] **Step 2: Run tests to verify RED**

```bash
python3 -m pytest tests/test_compensation.py tests/test_submission_attempts.py -k "compensation or schema_round_trip" -q
```

Expected: import failure because the package does not exist.

- [ ] **Step 3: Implement the dataclasses**

```python
# compensation/models.py
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class JobContext:
    posting_id: str
    company: str
    title: str
    location: str
    employment_type: str
    currency: str
    period: str

@dataclass(frozen=True)
class EvidenceObservation:
    url: str
    domain: str
    title: str
    low: Decimal | None
    high: Decimal | None
    point: Decimal | None
    currency: str
    period: str
    source_kind: str
    observed_at: int

@dataclass(frozen=True)
class CompensationResolution:
    context: JobContext
    amount: Decimal
    method: str
    evidence: tuple[EvidenceObservation, ...]
    researched_at: int
    expires_at: int
```

Because Python 3.9 does not support `X | None`, import `Optional` and use `Optional[Decimal]` in the actual implementation.

- [ ] **Step 4: Implement schema and exact cache serialization**

Use one table with context columns and JSON evidence:

```sql
CREATE TABLE IF NOT EXISTS compensation_evidence (
  cache_key TEXT PRIMARY KEY,
  posting_id TEXT NOT NULL,
  company TEXT NOT NULL,
  normalized_role TEXT NOT NULL,
  location TEXT NOT NULL,
  employment_type TEXT NOT NULL,
  currency TEXT NOT NULL,
  period TEXT NOT NULL,
  resolved_amount TEXT NOT NULL,
  method TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  researched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
)
```

`cache_key(context)` must SHA-256 a canonical JSON array of the exact normalized context fields. Serialize decimals as strings and reconstruct them as `Decimal`.

Call `ensure_compensation_schema(conn)` from `submission.database.connect_tracker` after existing schema hooks.

- [ ] **Step 5: Run focused tests to verify GREEN**

```bash
python3 -m pytest tests/test_compensation.py tests/test_submission_attempts.py -k "compensation or schema_round_trip" -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add compensation submission/database.py tests/test_compensation.py tests/test_submission_attempts.py
git commit -m "Add compensation evidence storage"
```

---

### Task 2: Normalize conservative observations and resolve evidence

**Files:**
- Create: `compensation/normalize.py`
- Create: `compensation/resolve.py`
- Modify: `tests/test_compensation.py`

**Interfaces:**
- Consumes: `JobContext`, `EvidenceObservation`.
- Produces: `extract_usd_observations(text, *, url, title, source_kind, observed_at) -> list[EvidenceObservation]`.
- Produces: `resolve_observations(context, observations, *, now) -> CompensationResolution | None`.
- Produces: `requested_period(question) -> str | None`.

- [ ] **Step 1: Write failing normalization tests**

```python
@pytest.mark.parametrize((text, expected), [
    ("$40-$50 per hour", ("40", "50", "hour")),
    ("USD 90,000 to 110,000 per year", ("90000", "110000", "year")),
    ("Pay is competitive", None),
    ("€50,000 per year", None),
    ("$50,000", None),  # period is ambiguous
])
def test_extract_usd_ranges_is_conservative(text, expected):
    rows = extract_usd_observations(
        text, url="https://levels.fyi/x", title="x", source_kind="market", observed_at=100,
    )
    if expected is None:
        assert rows == []
    else:
        assert (str(rows[0].low), str(rows[0].high), rows[0].period) == expected
```

Add tests for `requested_period`: hourly/rate labels return `hour`; annual/base salary return `year`; generic “desired compensation” returns `None`.

- [ ] **Step 2: Write failing resolution tests**

```python
def test_exact_employer_range_midpoint_wins():
    result = resolve_observations(context(), [employer("42", "58"), market("40", "50")], now=100)
    assert result.amount == Decimal("50")
    assert result.method == "employer_midpoint"


def test_two_independent_domains_resolve_market_median():
    rows = [market("40", "50", domain="levels.fyi"), market("44", "54", domain="indeed.com")]
    result = resolve_observations(context(), rows, now=100)
    assert result.amount == Decimal("47")  # median of 45 and 49, hourly rounded whole


@pytest.mark.parametrize("rows", [
    [market("40", "50", domain="levels.fyi")],
    [market("40", "50", domain="levels.fyi"), market("45", "55", domain="levels.fyi")],
    [market("20", "25"), market("70", "80", domain="indeed.com")],
    [market("40", "50", currency="EUR"), market("44", "54", domain="indeed.com")],
])
def test_insufficient_or_inconsistent_market_evidence_fails_closed(rows):
    assert resolve_observations(context(), rows, now=100) is None
```

Add annual/hourly conversion and annual nearest-$1,000 rounding cases.

- [ ] **Step 3: Run tests to verify RED**

```bash
python3 -m pytest tests/test_compensation.py -k "extract_usd or requested_period or employer_range or market_median or fails_closed or conversion" -q
```

Expected: import/attribute failures.

- [ ] **Step 4: Implement conservative extraction**

Use anchored currency and explicit period patterns. Do not parse bare numbers:

```python
_RANGE = re.compile(
    r"(?:USD\s*)?\$?\s*(?P<low>\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?:-|–|—|to)\s*(?:USD\s*)?\$?\s*(?P<high>\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<period>per\s+hour|hourly|/\s*hr|per\s+year|annually|annual)", re.I,
)
```

Require either `$` or literal `USD` in the full match. Map period tokens to `hour`/`year`; reject low > high, non-positive values, hourly values outside 5–500, and annual values outside 10,000–1,000,000.

- [ ] **Step 5: Implement deterministic resolution**

- Filter exact context compatibility before arithmetic.
- Prefer `source_kind == "employer"` and require exactly one compatible employer range.
- For market data, dedupe by domain and require at least two domains.
- Normalize all points to the requested period only when it is explicit.
- Reject max/min > 2.
- Use `statistics.median` over source midpoint/point estimates.
- Set `expires_at = now + 30 * 86400`.

- [ ] **Step 6: Run all compensation unit tests**

```bash
python3 -m pytest tests/test_compensation.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add compensation/normalize.py compensation/resolve.py tests/test_compensation.py
git commit -m "Resolve compensation from conservative evidence"
```

---

### Task 3: Add Tavily research orchestration and explicit CLI

**Files:**
- Create: `compensation/research.py`
- Create: `scripts/prepare_compensation.py`
- Create: `tests/test_compensation_cli.py`

**Interfaces:**
- Produces: `SearchProvider.search(query, include_domains) -> list[dict]` protocol.
- Produces: `TavilySearchProvider(api_key, timeout=15)`.
- Produces: `prepare_posting(conn, posting_id, provider, *, now) -> dict`.
- Produces CLI: `python3 scripts/prepare_compensation.py --db PATH --posting-id ID [--json]`.

- [ ] **Step 1: Write fake-provider orchestration tests**

Seed a copied tracker with a manual compensation blocker and monkeypatch `apply.jd.fetch_jd`:

```python
class FakeProvider:
    def __init__(self, results): self.results = results; self.queries = []
    def search(self, query, include_domains):
        self.queries.append((query, tuple(include_domains)))
        return self.results


def test_prepare_posting_uses_employer_range_without_search(conn):
    provider = FakeProvider([])
    result = prepare_posting(conn, "p1", provider, now=100,
                             fetch_jd=lambda _url: "Pay range $40-$50 per hour")
    assert result["status"] == "stored"
    assert result["amount"] == "45"
    assert provider.queries == []


def test_prepare_posting_searches_only_public_job_context(conn):
    provider = FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme intern", "content": "$40-$50 per hour"},
        {"url": "https://indeed.com/a", "title": "Acme intern", "content": "$44-$54 per hour"},
    ])
    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "No pay listed")
    query = provider.queries[0][0]
    assert "Acme" in query and "Software Engineer Intern" in query
    assert "david" not in query.lower() and "@" not in query
    assert result["status"] == "stored"
```

Add failures for absent posting, non-compensation blocker, no API key, timeout, malformed results, and insufficient evidence. Assert no evidence row is stored.

- [ ] **Step 2: Run tests to verify RED**

```bash
python3 -m pytest tests/test_compensation_cli.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement the provider with standard-library HTTP**

POST JSON to `https://api.tavily.com/search` using `urllib.request`. Request fields:

```python
payload = {
    "api_key": self.api_key,
    "query": query,
    "search_depth": "advanced",
    "include_domains": include_domains,
    "max_results": 8,
    "include_answer": False,
    "include_raw_content": False,
}
```

Return only dictionaries containing string `url`, `title`, and `content`. Enforce HTTPS and the approved-domain allowlist before passing results to normalization.

- [ ] **Step 4: Implement `prepare_posting`**

- Read exact posting ID, company, title, locations, URL, status, and `last_error`.
- Require manual/failed and a compensation-question marker.
- Derive `employment_type` from `track.infer_track(title)`.
- Derive requested period from the recorded unanswered label; if ambiguous, return `manual_period_unknown` with no cache write.
- Fetch the canonical JD and parse employer evidence first.
- Search only if employer evidence is absent.
- Resolve and call `store_resolution`; never update `postings`.
- Return a redacted JSON-safe summary with amount, period, method, and public source URLs.

- [ ] **Step 5: Implement the CLI**

Use `argparse` with repeatable `--posting-id`, required `--db`, and optional `--json`. Open through `connect_tracker(Path(args.db))`. Require `TAVILY_API_KEY` only when search is needed. Exit 0 with per-posting fail-closed results; exit 2 for invalid CLI/database shape.

- [ ] **Step 6: Run CLI tests and a fake-provider fixture round trip**

```bash
python3 -m pytest tests/test_compensation_cli.py tests/test_compensation.py -q
```

Expected: PASS with unchanged posting status/application/attempt counts.

- [ ] **Step 7: Commit**

```bash
git add compensation/research.py scripts/prepare_compensation.py tests/test_compensation_cli.py
git commit -m "Prepare source-backed compensation evidence"
```

---

### Task 4: Make QA consume fresh evidence without network access

**Files:**
- Modify: `submission/executor.py:82-162`
- Modify: `submit_worker.py:69-93`
- Modify: `apply/qa.py:1086-1587`
- Modify: `apply/qa.py:1598-2068`
- Modify: `tests/test_qa_policy.py`
- Modify: `tests/test_submission_dispatcher.py`

**Interfaces:**
- Consumes child payload keys: `posting_id`, `company`, `title`, `locations`, `tracker_db`.
- Produces environment variables: `JOBHUNT_POSTING_ID`, `JOBHUNT_COMPANY`, `JOBHUNT_JOB_TITLE`, `JOBHUNT_JOB_LOCATION`, `JOBHUNT_TRACKER_DB`.
- Consumes: `compensation.resolve.cached_answer_for_control(control, env, now) -> str | None`.

- [ ] **Step 1: Write failing payload propagation tests**

Update the dispatcher fake adapter assertion:

```python
assert payload["posting_id"] == row["posting_id"]
assert payload["company"] == row["company"]
assert payload["locations"] == row["locations"]
assert payload["tracker_db"].endswith("tracker.db")
```

Add a `submit_worker` test that imports the adapter after all five environment variables are set.

- [ ] **Step 2: Write failing cache-only QA tests**

```python
def test_numeric_compensation_is_allowed_only_from_exact_fresh_cache(monkeypatch, compensation_db):
    control = {"id": "comp", "label": "Desired hourly compensation", "options": []}
    set_job_env(monkeypatch, posting_id="p1", company="Acme", title="Software Engineer Intern",
                location="New York, NY", db=compensation_db)
    answers = qa.explicit_approved_answers([control], approved_answers=APPROVED)
    assert answers == [{"id_or_name": "comp", "answer": "47"}]
    assert not qa.answer_requires_manual(control, "47", approved_answers=APPROVED)
    assert qa.answer_requires_manual(control, "48", approved_answers=APPROVED)
```

Add stale, wrong posting, wrong company, wrong period, generic ambiguous label, and missing DB tests. Assert no HTTP/provider function is called.

- [ ] **Step 3: Run focused tests to verify RED**

```bash
python3 -m pytest tests/test_qa_policy.py tests/test_submission_dispatcher.py -k "compensation or payload" -q
```

Expected: payload key failures and numeric answer remains blocked.

- [ ] **Step 4: Propagate public posting context**

In `execute_claimed_posting`, include:

```python
{
    "posting_id": posting_id,
    "company": company,
    "title": title,
    "locations": _row_get(row, "locations", "") or "",
    "tracker_db": str(conn.execute("PRAGMA database_list").fetchone()[2]),
}
```

In `submit_worker.main`, set the five environment variables before `_adapter(...)` imports adapter modules.

- [ ] **Step 5: Implement cache-only QA lookup**

Add `cached_answer_for_control` to `compensation/resolve.py`:

- Determine period from label/options; return `None` if ambiguous.
- Build exact `JobContext` from environment.
- Open `JOBHUNT_TRACKER_DB` read-only with URI `mode=ro`.
- Call `load_resolution` with current time.
- Format only the resolved amount for the requested period.

In `explicit_approved_answers`, check compensation before model answers and use the cached value. In `_blocked_answer_is_approved`, require exact equality to the same cache lookup. Keep existing nonnumeric offered options permitted by compensation policy.

- [ ] **Step 6: Run focused tests to verify GREEN**

```bash
python3 -m pytest tests/test_qa_policy.py tests/test_submission_dispatcher.py -k "compensation or payload" -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add compensation/resolve.py submission/executor.py submit_worker.py apply/qa.py tests/test_qa_policy.py tests/test_submission_dispatcher.py
git commit -m "Use cached compensation evidence in application QA"
```

---

### Task 5: Add bounded hourly research and preview-first retriage

**Files:**
- Modify: `compensation/research.py`
- Modify: `drip.py:208-279`
- Modify: `submission/resolutions.py`
- Modify: `scripts/retriage_resolved_postings.py`
- Modify: `tests/test_resolution_retriage.py`
- Modify: `test_throughput.py`

**Interfaces:**
- Produces: `prepare_pending_compensation(conn, provider, *, limit=5, now=None) -> dict`.
- Produces: `compensation_retriage_candidates(conn, now) -> list[dict]`.
- Extends the existing preview/apply CLI without changing default safe limits or ATS filters.

- [ ] **Step 1: Write failing hourly-batch tests**

Seed six compensation-blocked manual rows and assert:

```python
summary = prepare_pending_compensation(conn, FakeProvider(results), limit=5, now=100)
assert summary["examined"] == 5
assert summary["stored"] == 5
assert conn.execute("select count(*) from postings where status='manual'").fetchone()[0] == 6
```

Add no-key behavior at the drip boundary: no provider call, no exception, and a redacted `disabled_missing_key` summary.

- [ ] **Step 2: Write failing retriage safety tests**

```python
def test_compensation_preview_is_read_only_and_apply_requires_fresh_exact_evidence(db):
    before = db_hash(db)
    preview = run_cli(db, "--preview", "--json")
    assert preview["compensation_candidates"] == 1
    assert db_hash(db) == before
    applied = run_cli(db, "--apply", "--json")
    assert applied["updated"] == 1
    assert posting_status(db, "p1") == "ready"
```

Add stale evidence, changed `last_error`, existing application, finished attempt, unsupported lane, and Oracle-held cases. Each must remain manual.

- [ ] **Step 3: Run tests to verify RED**

```bash
python3 -m pytest tests/test_resolution_retriage.py test_throughput.py -k "compensation" -q
```

Expected: missing function/summary failures.

- [ ] **Step 4: Implement bounded preparation**

Query only:

```sql
SELECT posting_id FROM postings
WHERE status IN ('manual','failed')
  AND lower(COALESCE(last_error,'')) GLOB '*compensation*'
   OR lower(COALESCE(last_error,'')) GLOB '*salary*'
ORDER BY COALESCE(last_attempt_at, first_seen), posting_id
LIMIT ?
```

Parenthesize status/reason conditions correctly. Call `prepare_posting` serially, catch each exception, and return counts without status updates.

In `drip.run`, invoke this after watcher/filter only when `TAVILY_API_KEY` exists. Keep `limit=5`; log a summary without source snippets.

- [ ] **Step 5: Implement compensation retriage candidates**

A candidate requires all of:

- posting status `manual` or `failed`;
- current `last_error` still indicates a required compensation blocker;
- fresh exact evidence for the posting context;
- no application row;
- no finished attempt with click/confirmation uncertainty;
- lane is automatic and not Oracle/Ashby/manual/unsupported;
- no canonical duplicate conflict.

Merge these candidates into `retriage_resolved_postings.py` output under a distinct `candidate_kind="compensation"`. Apply with `BEGIN IMMEDIATE` and CAS over posting ID, original status, original `last_error`, and attempt count. Preserve the existing default safe limit.

- [ ] **Step 6: Run retriage and throughput tests**

```bash
python3 -m pytest tests/test_resolution_retriage.py test_throughput.py -k "compensation or retriage or drip" -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add compensation/research.py drip.py submission/resolutions.py scripts/retriage_resolved_postings.py tests/test_resolution_retriage.py test_throughput.py
git commit -m "Research and retriage compensation blockers safely"
```

---

### Task 6: Verify integration boundaries and live read-only behavior

**Files:**
- Modify only for discovered regressions: compensation package/tests listed above.
- Write ignored evidence under: `out/compensation-acceptance-2026-08-25.json`.

**Interfaces:**
- Consumes: real public Tavily/provider boundary, copied tracker DB, production JD fetcher, production QA cache lookup.
- Produces: acceptance evidence with source URLs, normalized values, DB hashes/counts, and no submission mutation.

- [ ] **Step 1: Run focused and full tests**

```bash
python3 -m pytest tests/test_compensation.py tests/test_compensation_cli.py tests/test_qa_policy.py tests/test_resolution_retriage.py test_throughput.py -q
python3 -m pytest -q
git diff --check
```

Expected: all tests PASS and no whitespace errors.

- [ ] **Step 2: Select one current compensation-blocked posting read-only**

Open the live tracker with SQLite URI `mode=ro`, select one manual/failed row whose current error contains a compensation label, and record posting ID/company/title/location only in shell variables. Record live application and attempt counts and `PRAGMA integrity_check`.

- [ ] **Step 3: Copy the tracker and run one live research probe**

```bash
cp out/tracker.db "$JCODE_SCRATCH_DIR/compensation-acceptance.db"
python3 scripts/prepare_compensation.py \
  --db "$JCODE_SCRATCH_DIR/compensation-acceptance.db" \
  --posting-id "$POSTING_ID" --json
```

Require either:

- `stored` with at least one employer source or at least two independent approved market domains, or
- an explicit fail-closed result such as `insufficient_evidence`, `period_unknown`, or `provider_unavailable`.

Never treat a fail-closed result as acceptance of numeric automation.

- [ ] **Step 4: Exercise production QA against the copied cache**

Set `JOBHUNT_POSTING_ID`, `JOBHUNT_COMPANY`, `JOBHUNT_JOB_TITLE`, `JOBHUNT_JOB_LOCATION`, and `JOBHUNT_TRACKER_DB` to the copied DB. Call `qa.explicit_approved_answers` for the exact compensation label. If research stored valid evidence, assert the exact cached number is returned and approved; otherwise assert no numeric answer is returned.

- [ ] **Step 5: Verify no live mutation**

Re-read the live tracker in `mode=ro` and assert:

- application count unchanged;
- submission-attempt count unchanged;
- target status/attempt count unchanged;
- `PRAGMA integrity_check == 'ok'`.

Run the real retriage CLI only with `--preview --json`; confirm no Oracle release and hash unchanged.

- [ ] **Step 6: Save evidence and commit any acceptance correction**

Write the redacted result, source URLs, normalized observations, commands, before/after counts, and git SHA to the ignored evidence file. If a code correction was needed, reproduce it with a failing test, fix it, rerun focused/full suites, and commit only source/tests. Otherwise do not create an empty commit.
