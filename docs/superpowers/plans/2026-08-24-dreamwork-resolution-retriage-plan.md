# Dreamwork URL Resolution and Re-triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Dreamwork wrapper listings into safe final ATS URLs, preserve provenance, and requeue only technically blocked postings that remain eligible and unique.

**Architecture:** A pure resolver parses saved HTML, a cache table stores source and resolved URLs, and watcher ingestion reapplies cached resolutions before classification. A separate preview-first re-triage command performs canonical and attempt-ledger checks before compare-and-set state changes.

**Tech Stack:** Python 3.9 standard library, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-jobhunt-blocker-remediation-design.md`

## Global Constraints

- Never perform network I/O inside `classify_url`, `canonical_posting_key`, or a submission claim transaction.
- Accept only public `http` and `https` resolution targets.
- Preserve the wrapper source URL and a source-page hash.
- Never revive stale, closed, skipped, submitted, click-uncertain, CAPTCHA, spam, or user-skipped rows.
- Never requeue a posting with a finished submission attempt.
- Preserve canonical duplicate and applications-ledger checks.
- Preview mode must not mutate SQLite.
- Use TDD and commit after each independently reviewed task.
- Never stage `out/tracker.db`.
- Do not use Unicode em or en dashes in added prose.

## File structure

- Create `watcher/url_resolver.py`: pure Dreamwork parser, target validation, and cache record type.
- Create `submission/resolutions.py`: resolution schema, cached lookup, conflict-safe apply, and re-triage candidates.
- Modify `watcher/watch.py`: schema initialization and cached resolver integration.
- Modify `submission/identity.py`: reusable canonical conflict query.
- Create `scripts/retriage_resolved_postings.py`: JSON preview and explicit apply interface.
- Create `tests/fixtures/dreamwork/job-greenhouse.html`: deterministic wrapper fixture.
- Create `tests/fixtures/dreamwork/job-unsafe.html`: unsafe-target fixture.
- Create `tests/test_url_resolver.py`: parser and safety tests.
- Create `tests/test_resolution_retriage.py`: schema, cache, dedupe, and state tests.
- Modify `test_throughput.py`: watcher integration regression.

---

### Task 1: Pure Dreamwork parser and target validator

**Files:**
- Create: `watcher/url_resolver.py`
- Create: `tests/fixtures/dreamwork/job-greenhouse.html`
- Create: `tests/fixtures/dreamwork/job-unsafe.html`
- Create: `tests/test_url_resolver.py`

**Interfaces:**
- Produces: `ResolutionResult`
- Produces: `resolve_dreamwork_html(source_url: str, html: str) -> ResolutionResult`
- Produces: `validate_public_target(url: str) -> str`

- [ ] **Step 1: Add saved HTML fixtures**

The Greenhouse fixture contains:

```html
<html><body>
<a class="job-cta-secondary" href="https://job-boards.greenhouse.io/acme/jobs/1234567">
View original posting
</a>
</body></html>
```

The unsafe fixture points to `http://127.0.0.1/admin`.

- [ ] **Step 2: Write failing parser tests**

```python
def test_dreamwork_parser_extracts_original_posting(fixture_text):
    result = resolve_dreamwork_html(DREAMWORK_URL, fixture_text("job-greenhouse.html"))
    assert result.resolved_url == "https://job-boards.greenhouse.io/acme/jobs/1234567"
    assert result.resolver == "dreamwork-original-v1"
    assert result.error == ""
    assert len(result.source_hash) == 64


def test_dreamwork_parser_rejects_private_target(fixture_text):
    result = resolve_dreamwork_html(DREAMWORK_URL, fixture_text("job-unsafe.html"))
    assert result.resolved_url is None
    assert result.error == "resolved target is not a public http(s) URL"
```

Also cover relative links, missing links, `javascript:`, `file:`, credentials in authority, `localhost`, private IPv4, private IPv6, and an anchor whose text does not identify the original posting.

- [ ] **Step 3: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_url_resolver.py`

Expected: FAIL because `watcher.url_resolver` does not exist.

- [ ] **Step 4: Implement the pure resolver**

```python
@dataclass(frozen=True)
class ResolutionResult:
    source_url: str
    resolved_url: str | None
    resolver: str
    source_hash: str
    error: str


def validate_public_target(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("resolved target is not a public http(s) URL")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("resolved target is not a public http(s) URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("resolved target is not a public http(s) URL")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
```

Use `html.parser.HTMLParser` to collect anchors whose class includes `job-cta-secondary` and whose normalized text is `View original posting`. Resolve relative links with `urljoin`.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python3 -m pytest -q tests/test_url_resolver.py`

Expected: all tests PASS with no network calls.

- [ ] **Step 6: Commit**

```bash
git add watcher/url_resolver.py tests/fixtures/dreamwork tests/test_url_resolver.py
git commit -m "Parse Dreamwork original posting links"
```

### Task 2: Resolution cache and canonical conflict interface

**Files:**
- Create: `submission/resolutions.py`
- Modify: `submission/identity.py:77-130`
- Modify: `submission/database.py`
- Create: `tests/test_resolution_retriage.py`
- Modify: `tests/test_submission_identity.py`

**Interfaces:**
- Produces: `ensure_resolution_schema(conn) -> None`
- Produces: `cached_resolution(conn, posting_id, source_url) -> str | None`
- Produces: `record_resolution(conn, result) -> None`
- Produces: `canonical_conflict_reason(conn, posting_id, url) -> str | None`

- [ ] **Step 1: Write failing schema and conflict tests**

```python
def test_resolution_schema_is_idempotent(conn):
    ensure_resolution_schema(conn)
    ensure_resolution_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(posting_url_resolutions)")}
    assert columns == {
        "posting_id", "source_url", "resolved_url", "resolver",
        "resolved_at", "last_error", "source_hash",
    }


def test_canonical_conflict_detects_applied_alias(conn):
    seed_applied(conn, "done", "https://job-boards.greenhouse.io/acme/jobs/1234567")
    assert canonical_conflict_reason(
        conn,
        "wrapper",
        "https://boards.greenhouse.io/acme/jobs/1234567",
    ) == "canonical posting already applied"
```

Add tests for an active submitting claim, a terminal stale alias, the current row itself, and a distinct ATS ID.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py tests/test_submission_identity.py -k 'resolution or conflict'`

Expected: FAIL because schema and conflict interfaces do not exist.

- [ ] **Step 3: Add the idempotent cache schema**

```sql
CREATE TABLE IF NOT EXISTS posting_url_resolutions (
    posting_id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    resolved_url TEXT,
    resolver TEXT NOT NULL,
    resolved_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posting_url_resolutions_target_idx
ON posting_url_resolutions(resolved_url);
```

Initialize it from `connect_tracker` and `watch.init_db` without committing a caller-owned transaction.

- [ ] **Step 4: Implement cache semantics**

`cached_resolution` returns a target only when both `posting_id` and `source_url` match and the cached row has a nonempty `resolved_url`. `record_resolution` uses `INSERT ... ON CONFLICT(posting_id) DO UPDATE` and stores only sanitized errors up to 500 characters.

- [ ] **Step 5: Implement reusable canonical conflict checks**

`canonical_conflict_reason` computes one wanted key and scans:

1. rows joined to `applications`
2. other rows in `submitting` or `sprinting`
3. other terminal rows with `outcome IN ('stale','submitted','deduplicated')` or `status IN ('submitted','skipped')`

Return exact reason strings or `None`. Do not fetch URLs or start transactions inside this function.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py tests/test_submission_identity.py tests/test_submission_attempts.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add submission/resolutions.py submission/identity.py submission/database.py tests/test_resolution_retriage.py tests/test_submission_identity.py
git commit -m "Store resolved posting URL provenance"
```

### Task 3: Integrate cached resolution into watcher ingestion

**Files:**
- Modify: `watcher/watch.py:165-331`
- Modify: `test_throughput.py`
- Modify: `tests/test_resolution_retriage.py`

**Interfaces:**
- Produces: `resolve_dreamwork_postings(conn, fetch_page) -> dict`
- Consumes: `resolve_dreamwork_html`, `cached_resolution`, `record_resolution`, and `canonical_conflict_reason`

- [ ] **Step 1: Write failing watcher integration tests**

```python
def test_watcher_reapplies_cached_resolution_before_filter(tmp_db):
    seed_manual_dreamwork(tmp_db, posting_id="dw-1", url=DREAMWORK_URL)
    seed_resolution(tmp_db, "dw-1", DREAMWORK_URL, GREENHOUSE_URL)
    result = resolve_dreamwork_postings(tmp_db, fetch_page=pytest.fail)
    assert result == {"cached": 1, "resolved": 0, "failed": 0, "conflicts": 0}
    assert posting_url(tmp_db, "dw-1") == GREENHOUSE_URL


def test_watcher_resolution_conflict_never_rewrites_or_requeues(tmp_db):
    seed_applied(tmp_db, "done", GREENHOUSE_URL)
    seed_manual_dreamwork(tmp_db, posting_id="dw-1", url=DREAMWORK_URL)
    result = resolve_dreamwork_postings(tmp_db, fetch_page=lambda _: GREENHOUSE_HTML)
    assert result["conflicts"] == 1
    assert posting_status(tmp_db, "dw-1") == "manual"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py test_throughput.py -k dreamwork`

Expected: FAIL because `resolve_dreamwork_postings` does not exist.

- [ ] **Step 3: Implement resolution application**

For each Dreamwork row whose current URL is a wrapper:

1. use a matching cached resolution when available
2. otherwise fetch only the known Dreamwork source URL and parse it
3. record success or failure outside a submission claim transaction
4. run `canonical_conflict_reason`
5. update `postings.url` only when no conflict exists
6. preserve manual status until the explicit re-triage step

Commit once per bounded batch, not once per field.

- [ ] **Step 4: Call the resolver from `watch.run`**

Run it after upsert and before filter receives current posting IDs. Print one compact summary. Cached entries must cause no page fetch. Resolver exceptions fail one row closed and do not stop other sources.

- [ ] **Step 5: Add a no-hot-path-network regression**

Monkeypatch every network helper to raise, then call `classify_url`, `canonical_posting_key`, and `posting_already_applied` on a Dreamwork and resolved URL. Assert no network helper was invoked.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py test_throughput.py tests/test_submission_identity.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add watcher/watch.py tests/test_resolution_retriage.py test_throughput.py tests/test_submission_identity.py
git commit -m "Resolve Dreamwork wrappers during ingestion"
```

### Task 4: Preview-first technical-debt re-triage command

**Files:**
- Modify: `submission/resolutions.py`
- Create: `scripts/retriage_resolved_postings.py`
- Modify: `tests/test_resolution_retriage.py`

**Interfaces:**
- Produces: `retriage_candidates(conn) -> list[dict]`
- Produces: `apply_retriage(conn, posting_ids: list[str]) -> dict`
- CLI: `python3 scripts/retriage_resolved_postings.py --db PATH --preview|--apply --json`

- [ ] **Step 1: Write failing candidate and mutation tests**

Seed:

- one resolved Greenhouse row with `last_error='no adapter for other'`
- one resolved SmartRecruiters row with the same reason
- one click-uncertain row
- one stale row
- one row with a finished attempt
- one applied canonical alias

Assert preview returns only the first two. Assert apply moves Greenhouse to `queued`, keeps SmartRecruiters `manual` with `prepared for manual completion: smartrecruiters`, and leaves every excluded row byte-for-byte unchanged.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py -k retriage`

Expected: FAIL because candidate and apply functions do not exist.

- [ ] **Step 3: Implement eligibility rules**

Use exact technical prefixes:

```python
TECHNICAL_MANUAL_PREFIXES = (
    "no adapter for other",
    "no adapter for icims",
)
```

Require `status='manual'`, no finished attempt, no application-ledger conflict, no active canonical claim, and a resolution target. Derive the destination from `preparation_destination`.

- [ ] **Step 4: Implement compare-and-set apply**

Start `BEGIN IMMEDIATE`, recompute every candidate, and update only when `posting_id`, `status='manual'`, and the original `last_error` still match. Commit the full selected batch or roll back on any exception.

- [ ] **Step 5: Implement deterministic JSON CLI**

Preview is default and exits zero. `--apply` is mutually exclusive with `--preview`. Output only posting ID, company, title, source URL, resolved URL, ATS, destination, and reason. Never print credentials, answers, or artifact paths.

- [ ] **Step 6: Add subprocess-level CLI tests**

Run the exact documented command against a temporary DB. Compare its file hash before and after preview. Assert apply changes only expected rows and a second apply reports zero changes.

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_resolution_retriage.py`

Run: `python3 -m pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add submission/resolutions.py scripts/retriage_resolved_postings.py tests/test_resolution_retriage.py
git commit -m "Requeue safely resolved technical debt"
```

### Task 5: Live Dreamwork preview and bounded rollout

**Files:**
- Modify: `README.md`
- Modify: `docs/system-design.html`
- Test: all files from Tasks 1 through 4

**Interfaces:**
- Consumes: resolution and re-triage CLIs
- Produces: backup, preview artifact, applied summary, and invariant evidence

- [ ] **Step 1: Document operator commands and invariants**

Document preview-first usage, cache semantics, provenance, canonical collision behavior, and how to rerun safely.

- [ ] **Step 2: Run full tests and a temporary-DB CLI acceptance pass**

Run:

```bash
python3 -m pytest -q
python3 scripts/retriage_resolved_postings.py --db "$JCODE_SCRATCH_DIR/resolution-acceptance.db" --preview --json
```

Expected: full suite passes and preview leaves the temporary DB hash unchanged.

- [ ] **Step 3: Create a live SQLite backup and integrity proof**

Use SQLite `.backup` into `out/backups/pre-dreamwork-resolution-<UTC>.db`, set mode 0600, and verify `PRAGMA integrity_check` on both files.

- [ ] **Step 4: Run live resolution preview**

Produce counts by target ATS and conflict reason. Manually inspect at least one Greenhouse, Lever, Workday, SmartRecruiters, Ashby, Oracle, corporate, missing-link, and unsafe-target sample. Do not apply yet.

- [ ] **Step 5: Apply re-triage once**

Run the apply command. Immediately verify:

- zero active canonical duplicate groups
- zero active aliases of application-ledger rows
- zero offseason active rows
- zero finished-attempt rows requeued
- unresolved and unsafe rows remain manual

- [ ] **Step 6: Observe the resident pipeline**

Confirm newly queued automatic rows move through tailoring and dispatcher lanes. Record confirmed, manual, stale, and failed outcomes by resolved ATS.

- [ ] **Step 7: Sync the Sheet and read back counts**

Run `sheet_tracker.py` and verify Pipeline plus Manual Actions counts match SQLite.

- [ ] **Step 8: Commit documentation and final checks**

```bash
git add README.md docs/system-design.html
git commit -m "Document source URL resolution workflow"
```

Run `git diff --check`, full pytest, SQLite integrity, and `git status --short`. Confirm `out/tracker.db` remains unstaged.
