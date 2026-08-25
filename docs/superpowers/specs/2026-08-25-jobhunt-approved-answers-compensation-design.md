# Jobhunt approved answers and compensation evidence design

Date: 2026-08-25
Status: approved in chat, pending written-spec review

## Context

The application pipeline already separates public profile facts from user-approved sensitive answers in `profile/application_answers.yaml`. `apply/qa.py` exposes only relevant answer-bank sections and fails closed on sensitive questions. Numeric compensation is intentionally blocked unless it is an offered form option, even though the approved policy says to use employer or market guidance. This leaves otherwise automatable applications in manual status.

David supplied the remaining personal facts and approved a job-specific compensation strategy. The system must persist those facts, use them only when the question matches, and never invent salary numbers.

## Goals

1. Persist the newly approved facts in the canonical application answer bank.
2. Resolve required numeric compensation questions from fresh, job-specific public evidence.
3. Prefer an employer-published range. Otherwise use the median of comparable market observations, including Levels.fyi when available.
4. Keep research outside the browser adapter and retain complete provenance.
5. Fail closed when evidence is missing, stale, mismatched, non-US/non-USD, or internally inconsistent.
6. Remove only the blockers supported by approved facts or sufficient evidence.

## Non-goals

- Do not guess compensation from a universal default.
- Do not send candidate identity, contact details, resume contents, or application answers to a search provider.
- Do not bypass CAPTCHA, account verification, or submission safety gates.
- Do not auto-release Oracle or other held queues as part of this work.
- Do not convert foreign currencies in v1.
- Do not treat search snippets as employer-authored evidence unless the URL is an employer-controlled job page.

## Approved facts

The implementation will encode these exact facts:

- Security clearance: none.
- Prior Point72 application: no.
- Prior Akuna Capital application/interview: no, reaffirming the existing record.
- US Department of Defense employment on or after January 28, 2008: no.
- Candidate, immediate-family, or business-partner government-entity employment: no.
- Relatives employed by Availity: no.
- Relatives employed by American Fidelity: no.
- NeurIPS 2026 attendance: no.
- English proficiency: fluent.
- Unofficial transcript: available and approved for application upload.
- Current pending offer: Soren, Founding Engineer. Preserve the existing unverified deadline note rather than inventing an exact contractual date.
- Default desired start date: May 15, 2027, the deterministic normalization of “mid-May.”

These values belong in structured sections of `profile/application_answers.yaml`, not in prompt-only prose. Company-specific facts remain keyed by normalized company name so they cannot leak to similarly named employers.

## Architecture

### 1. Canonical answer bank

Extend existing structured sections rather than introduce a second profile file:

- `legal.security_clearance`
- `legal.us_dod_employment_after_2008_01_28`
- `legal.self_or_family_or_business_partner_government_employment`
- `professional.english_proficiency`
- `documents.unofficial_transcript.available`
- `documents.unofficial_transcript.application_upload_authorized`
- `events.neurips_2026.attending`
- `availability.default_start_date`
- `company_facts.Point72.prior_application`
- `company_facts.Availity.household_employment`
- `company_facts.American Fidelity.household_employment`

Update `apply/qa.py` relevance selection and approval checks only for exact matching question families. A missing structured fact remains manual. Generic family, government, or prior-application questions must not borrow a company-specific answer.

### 2. Compensation evidence resolver

Add an isolated `compensation/` package with three public boundaries:

- `research.py`: obtains public search results through a provider interface.
- `normalize.py`: extracts and normalizes numeric observations.
- `resolve.py`: chooses an answer only when the evidence contract is satisfied.

The initial search provider is Tavily Search API behind a small interface. Tavily was selected because its Python API returns source URLs and snippets and supports domain-restricted queries. The provider key is read from `TAVILY_API_KEY` through the existing runtime-secret mechanism. Tests use a fake provider. No candidate PII is included in queries.

Search queries contain only normalized public job context: company, title/role family, location, employment type, and compensation unit. The allowlist is the employer career domain plus `levels.fyi`, `glassdoor.com`, `indeed.com`, `ziprecruiter.com`, and `salary.com`. New market domains require a code-reviewed allowlist change. Raw provider responses are treated as untrusted input.

### 3. Evidence contract and cache

Add a `compensation_evidence` table to the existing tracker SQLite database:

- `cache_key` primary key
- posting ID, company, normalized role, location, employment type
- requested currency and period (`hour`, `year`)
- resolved amount and confidence method
- structured evidence JSON with URL, domain, title, extracted low/high/point, currency, period, and source timestamp
- researched-at and expires-at timestamps

The cache key includes company, normalized role, location, employment type, requested currency, and period. Evidence is valid for 30 days. Writes use short SQLite transactions and never alter posting status by themselves.

### 4. Resolution hierarchy

1. **Exact employer range:** Fetch the canonical job description through the existing `apply.jd.fetch_jd` boundary. If the employer-controlled posting itself gives a USD range for the same role/location/employment type, use its midpoint. A search snippet pointing at an employer domain is not sufficient without matching text from the fetched posting.
2. **Market median:** Otherwise require at least two independent approved domains with compatible role family, location scope, employment type, USD currency, and period. Convert annual to hourly with 2,080 hours only when the form explicitly asks hourly; convert hourly to annual with the same factor only when it explicitly asks annual. Use the median of normalized source midpoints/point estimates.
3. **No numeric evidence:** Prefer an exact “open,” “market rate,” or “negotiable” option offered by the form. If the field requires a number, keep the application manual.

Round hourly values to the nearest whole dollar and annual values to the nearest $1,000. Reject negative values, implausible units, observations with no source URL, duplicate domains, and evidence whose maximum normalized value exceeds twice its minimum. Non-USD questions remain manual in v1.

### 5. Pipeline integration

Research does not run inside Playwright adapters.

- Add `scripts/prepare_compensation.py --posting-id ...` for one or more postings.
- The periodic revision path invokes it for manual/failed rows whose `last_error` contains a required compensation question.
- Successful research stores evidence only. Existing controlled retriage logic decides whether a row is eligible to become ready.
- `submit_worker.py` exports posting identity and public job context to `apply/qa.py`.
- When QA encounters a compensation control, it performs a cache-only lookup. A matching fresh evidence record authorizes that exact numeric answer. Missing or mismatched evidence remains manual.

This asynchronous two-pass design avoids search latency and nondeterminism during form filling. The first encounter may become manual; the next revision cycle researches it and enables a controlled retry.

## Error handling and safety

- Provider timeout, missing API key, malformed JSON, blocked salary page, or no sufficient evidence: record a diagnostic and leave the posting manual.
- Search results cannot authorize an answer until normalization and evidence checks pass.
- Compensation evidence never changes `click_attempted`, `submission_uncertain`, or application confirmation semantics.
- Logs may include company, title, location, amount, and public source URLs. They must not include candidate PII or application secrets.
- Cache-only QA resolution prevents a provider outage from stalling a browser worker.
- Controlled retriage remains preview-first and independently gated from evidence creation.

## Testing

### Answer-bank tests

- Each approved fact resolves representative exact questions.
- Point72, Akuna, Availity, and American Fidelity facts do not leak across company contexts.
- Government and DoD questions resolve only from their exact structured fields.
- Transcript upload authorization does not answer unrelated document-consent questions.
- “Mid-May” is rendered as `05/15/2027` only for start-date questions.

### Compensation tests

- Employer range midpoint wins over market sources.
- Two independent comparable sources produce the expected median.
- Duplicate domains, stale data, unit ambiguity, non-USD data, role/location mismatch, extreme spread, provider failure, and missing API key all fail closed.
- Hourly/annual conversion and rounding are deterministic.
- QA accepts a numeric amount only when a fresh cache record matches the posting and control period.
- Offered “market rate” options remain usable without numeric evidence.
- Evidence creation alone never makes a posting ready.

### Acceptance checks

- Run focused answer-policy and compensation tests, then the full suite.
- Exercise `prepare_compensation.py` with a fake provider and a copied fixture database.
- Run a live network research probe for one current compensation-blocked posting using a copied fixture database, inspect source URLs and normalized observations, and do not release or submit it.
- Exercise the production QA answer-resolution interface against that cached evidence in dry-run mode.
- Verify tracker integrity, unchanged application count, and no new submission attempts.

## Rollout

1. Land answer-bank facts and deterministic policy tests.
2. Land compensation schema, normalization, resolver, CLI, and provider adapter disabled unless `TAVILY_API_KEY` exists.
3. Run one read-only live compensation evidence probe.
4. Enable the periodic research step for compensation-blocked rows only.
5. Keep retriage preview-only until evidence quality is reviewed; do not release Oracle in this rollout.

## Success criteria

- All supplied facts answer their intended questions without cross-company leakage.
- Required numeric compensation is filled only from a fresh matching evidence record.
- Every numeric answer has retained public-source provenance and deterministic calculation.
- Missing or questionable evidence produces a manual result, never a fabricated amount.
- No submission or queue release occurs as a side effect of research.
- Focused, full-suite, live read-only, and tracker-integrity checks pass.
