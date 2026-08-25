# Task 3 Report: Render and verify approved answers deterministically

Status: complete
SHA: f620782

## Changed files

Committed in f620782:
- `apply/qa.py`
- `tests/test_qa_policy.py`

Ignored and untracked as required:
- `profile/application_answers.yaml` remains ignored, shown as `!! profile/application_answers.yaml` after commit.

## Implementation summary

- Added exact deterministic rendering for approved August 25 facts:
  - security clearance
  - Department of Defense employment after 2008-01-28
  - self/family/business-partner government employment
  - English proficiency
  - NeurIPS 2026 attendance
  - unofficial-transcript upload authorization
  - default start date
- Added exact verifier checks for the same fact families so approved values are accepted and contradictory values remain manual.
- Replaced broad clearance approval behavior with exact option/text comparison.
- Added hard-block policy coverage for these sensitive fact families so wrong model-provided values do not pass through after verifier rejection.
- Kept official-transcript authorization separate from unofficial-transcript authorization by rendering/verifying only `unofficial transcript` upload-consent wording.
- Tightened company matching so company facts do not leak from an approved company to a similarly named employer, while preserving explicit third-party company questions such as Deloitte household-employment questions inside another employer's form.

## RED evidence

After adding tests before production implementation, ran:

```bash
python3 -m pytest tests/test_qa_policy.py -k "august_25_facts_render or official_transcript or company_scoped_prior_application or point72 or availity or american_fidelity" -q
```

Observed expected RED:

```text
FF....F.                                                                 [100%]
FAILED tests/test_qa_policy.py::test_august_25_facts_render_exactly_and_wrong_values_remain_manual
FAILED tests/test_qa_policy.py::test_official_transcript_does_not_borrow_unofficial_upload_authorization
FAILED tests/test_qa_policy.py::test_company_scoped_prior_application_does_not_leak_to_similarly_named_employer
3 failed, 5 passed, 94 deselected in 0.21s
exit code 1
```

Failure reasons matched the missing Task 3 behavior:
- Renderer returned no August 25 deterministic approved facts.
- Official transcript wording borrowed transcript upload approval in verifier flow.
- American Fidelity facts leaked to the similarly named American Fidelity National Bank context.

## GREEN evidence

Focused Task 3 superset after implementation:

```bash
python3 -m pytest tests/test_qa_policy.py -k "august_25_facts_render or official_transcript or company_scoped_prior_application or point72 or availity or american_fidelity" -q
```

```text
8 passed, 94 deselected in 0.11s
exit code 0
```

Full policy test file:

```bash
python3 -m pytest tests/test_qa_policy.py -q
```

```text
102 passed in 0.47s
exit code 0
```

Exact command from task brief:

```bash
python3 -m pytest tests/test_qa_policy.py -k "august_25_facts_render or point72 or availity or american_fidelity" -q
```

```text
3 passed, 99 deselected in 0.10s
exit code 0
```

Whitespace check:

```bash
git diff --check
```

```text
exit code 0
```

Post-commit verification:

```bash
git status --short && git status --short --ignored profile/application_answers.yaml && git show --stat --oneline --name-only HEAD
```

```text
!! profile/application_answers.yaml
f620782 Answer approved application facts deterministically
apply/qa.py
tests/test_qa_policy.py
exit code 0
```

## Self-review

- Re-read Task 3 brief and Task 2 deferred minors before implementation.
- Confirmed tests use minimal sanitized dictionaries only and do not load the private profile answer bank.
- Reviewed the final diff for `apply/qa.py` and `tests/test_qa_policy.py` before commit.
- Confirmed private `profile/application_answers.yaml` remains ignored/untracked.
- Confirmed commit contains only `apply/qa.py` and `tests/test_qa_policy.py`.
- Addressed the full-suite regression caused by an over-tight company-context check by preserving explicit third-party company question matching while blocking similarly named context leakage.
- Did not touch live DBs, services, browsers, networks, queues, or submissions.
- Did not spawn agents.

## Concerns

None open. File activity warnings indicated nearby edits from another agent; I reviewed the final diff and committed only this Task 3 policy/test surface.

## Fix round 1/5 — macaque review

Status: complete

### Findings addressed

- C1: no-clearance synonyms now require an approved no-clearance fact. Missing clearance and mismatched non-none clearance values stay manual.
- I1: non-government wording no longer borrows government-employment facts.
- I2: empty company context no longer authorizes company-specific facts from question substring alone.
- I3: company equality now ignores only ordinary legal suffixes such as LLC/Inc./Corp. Descriptive suffixes such as National Bank remain distinct. Added Availity LLC positive and American Fidelity National Bank negative coverage.
- I4: internship start date now renders exactly from `availability.summer_2027.start` as `05/15/2027` and contradictory dates remain manual.
- M1: removed the duplicate unreachable clearance verifier branch.
- M2: left broad transcript matching fail-closed.

### RED evidence

After adding tests first, ran:

```bash
python3 -m pytest tests/test_qa_policy.py -k "clearance_no_clearance or non_government_employment or empty_company_context or company_context_accepts or internship_start_date" -q
```

Observed expected RED:

```text
FFFFF                                                                    [100%]
FAILED tests/test_qa_policy.py::test_clearance_no_clearance_synonyms_require_approved_no_clearance_fact
FAILED tests/test_qa_policy.py::test_non_government_employment_wording_stays_manual
FAILED tests/test_qa_policy.py::test_empty_company_context_does_not_authorize_question_substring_company_fact
FAILED tests/test_qa_policy.py::test_company_context_accepts_ordinary_legal_suffix_but_not_descriptive_suffix
FAILED tests/test_qa_policy.py::test_internship_start_date_renders_exactly_and_rejects_contradictions
5 failed, 102 deselected in 0.23s
exit code 1
```

### GREEN evidence

Focused fix-round tests:

```bash
python3 -m pytest tests/test_qa_policy.py -k "clearance_no_clearance or non_government_employment or empty_company_context or company_context_accepts or internship_start_date" -q
```

```text
5 passed, 102 deselected in 0.12s
exit code 0
```

Full policy suite:

```bash
python3 -m pytest tests/test_qa_policy.py -q
```

```text
107 passed in 0.48s
exit code 0
```

Whitespace check:

```bash
git diff --check
```

```text
exit code 0
```

### Self-review

- Reviewed final diff for `apply/qa.py` and `tests/test_qa_policy.py`.
- Confirmed `profile/application_answers.yaml` remains ignored/untracked via `!! profile/application_answers.yaml`.
- No live systems, browsers, networks, queues, submissions, DBs, or agents were used.

### Concerns

None open.
