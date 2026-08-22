# Task 2 Report: Replace company-wide suppression with canonical posting identity

## Implementation details
- Added `submission/identity.py` with the declared interface:
  - `canonical_posting_key(posting_id: str, url: str) -> str`
  - `posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool`
  - `claim_submission(conn, *, posting_id: str, url: str, from_status: str = "ready") -> str`
- Canonical posting keys are SHA-256 hashes of `apply.jd.canonical_application_url(url)` with fragment removal and trailing slash normalization.
- Added ATS-specific normalization for canonical identity where wrapper parameters should not distinguish jobs:
  - Ashby: strips `/application`, query strings, fragments, and normalizes host casing.
  - Greenhouse: keeps only stable `for` and `token` query identity when a token is present.
- Replaced the old company-wide duplicate guard in `submit.py` with canonical posting checks.
- Preserved `BEGIN IMMEDIATE`, compare-and-set status transitions, and the `applications` primary key behavior.
- Kept the existing `submit._claim_submission` compatibility wrapper, now delegating to `submission.identity.claim_submission` and accepting `url` instead of `company`.
- Updated `sprint.py` to pass `r["url"]` into the shared claim path and to handle `posting_claimed` rather than the removed `company_claimed` path.
- Did not touch or commit `out/tracker.db`.

## Files changed
- Created: `submission/identity.py`
- Created: `tests/test_submission_identity.py`
- Modified: `submit.py`
- Modified: `sprint.py`
- Modified: `test_submit.py`

## TDD evidence

### RED command
```bash
python3 -m pytest -q tests/test_submission_identity.py test_submit.py -k 'company or canonical or ledger'
```

### RED output
```text
==================================== ERRORS ====================================
______________ ERROR collecting tests/test_submission_identity.py ______________
ImportError while importing test module '/Users/davidcui824/jobhunt/.worktrees/hybrid-dispatcher/tests/test_submission_identity.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/importlib/__init__.py:127: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_submission_identity.py:3: in <module>
    from submission.identity import canonical_posting_key, posting_already_applied
E   ModuleNotFoundError: No module named 'submission.identity'
=========================== short test summary info ============================
ERROR tests/test_submission_identity.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
19 deselected, 1 error in 0.11s
```

### RED reason
- The new tests required the declared `submission.identity` interface before production code existed.
- Additional updated behavior tests expected distinct roles at the same company to submit separately and same canonical posting mirrors to submit only once, which the pre-change company-wide guard could not satisfy.

### GREEN focused command
```bash
python3 -m pytest -q tests/test_submission_identity.py test_submit.py -k 'company or canonical or ledger'
```

### GREEN focused output
```text
..........                                                               [100%]
10 passed, 20 deselected in 0.12s
```

### Required submission slice command
```bash
python3 -m pytest -q test_submit.py tests/test_submission_identity.py test_throughput.py
```

### Required submission slice output
```text
..............................................                           [100%]
46 passed in 0.16s
```

### Full suite command
```bash
python3 -m pytest -q
```

### Full suite output
```text
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 74%]
........................................................................ [ 99%]
.                                                                        [100%]
=============================== warnings summary ===============================
../../../Library/Python/3.9/lib/python/site-packages/google/oauth2/__init__.py:40
  /Users/davidcui824/Library/Python/3.9/lib/python/site-packages/google/oauth2/__init__.py:40: FutureWarning: You are using a Python version 3.9 past its end of life. Google will update google-auth with critical bug fixes on a best-effort basis, but not with any other fixes or features. Please upgrade your Python version, and then update google-auth.
    warnings.warn(eol_message.format("3.9"), FutureWarning)

../../../Library/Python/3.9/lib/python/site-packages/google/auth/__init__.py:54
  /Users/davidcui824/Library/Python/3.9/lib/python/site-packages/google/auth/__init__.py:54: FutureWarning: You are using a Python version 3.9 past its end of life. Google will update google-auth with critical bug fixes on a best-effort basis, but not with any other fixes or features. Please upgrade your Python version, and then update google-auth.
    warnings.warn(eol_message.format("3.9"), FutureWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
289 passed, 2 warnings in 1.43s
```

### Additional checks
```bash
git diff --check
```
Result: passed with no output.

## Self-review
- Verified no remaining `_company_already_applied` or `company_claimed` production paths remain in the submission/sprint claim flow.
- Verified `submit_ready` no longer excludes rows merely because another application exists at the same normalized company.
- Verified canonical mirror rows are still suppressed through `posting_already_applied` and claim-time `posting_claimed` handling.
- Verified the claim helper still uses `BEGIN IMMEDIATE`, rolls back on duplicate/active claims, commits compare-and-set transitions, and does not alter the `applications` primary key schema.
- Fixed a comment grammar issue found during diff review.

## Concerns
- Full suite passes. Only concern is pre-existing dependency/runtime warning that Python 3.9 is past Google auth's support window.


## Fix Round 1

### What changed
- Removed `apply.jd.canonical_application_url` from `submission.identity.canonical_posting_key`, making the submission identity path deterministic and network-free.
- Added local identity normalization for supported wrapper cases used by submission dedupe.
- Greenhouse identity now hashes `greenhouse:token:<token>` for both wrapper `gh_jid` URLs and direct Greenhouse `token` URLs, ignoring wrapper host and board slug availability.
- Preserved exact-posting dedupe and distinct-role behavior through the existing canonical-key comparisons.
- Did not address deferred minor findings for losing-row final-status assertion or Lever wrapper query normalization.

### RED evidence
```bash
python3 -m pytest -q tests/test_submission_identity.py -k 'greenhouse or network'
```

```text
.F                                                                       [100%]
=================================== FAILURES ===================================
______ test_greenhouse_token_key_is_independent_of_board_and_wrapper_host ______

    def test_greenhouse_token_key_is_independent_of_board_and_wrapper_host() -> None:
        wrapper = "https://company.example/jobs/software-engineer?gh_jid=1234567"
        board_direct = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1234567"
        fallback_direct = "https://boards.greenhouse.io/embed/job_app?token=1234567"
>       assert canonical_posting_key("wrapper", wrapper) == canonical_posting_key("board", board_direct)
E       AssertionError: assert 'a299beb461ac...d739755f8f2db' == '29a6e8fb4146...fc0721432ef05'
E         
E         - 29a6e8fb4146971c6ca75cd9f383a8bb4ff9641e2a6661e6669fc0721432ef05
E         + a299beb461ac9bdd2d7f14fc72038d4583bcd236705abd8164dd739755f8f2db

tests/test_submission_identity.py:45: AssertionError
=========================== short test summary info ============================
FAILED tests/test_submission_identity.py::test_greenhouse_token_key_is_independent_of_board_and_wrapper_host
1 failed, 1 passed, 2 deselected in 0.09s
```

### Test commands and outputs
```bash
python3 -m pytest -q tests/test_submission_identity.py -k 'greenhouse or network'
```

```text
..                                                                       [100%]
2 passed, 2 deselected in 0.04s
```

```bash
python3 -m pytest -q tests/test_submission_identity.py test_submit.py -k 'company or canonical or ledger or greenhouse or network'
```

```text
............                                                             [100%]
12 passed, 20 deselected in 0.11s
```

```bash
python3 -m pytest -q test_submit.py tests/test_submission_identity.py test_throughput.py
```

```text
................................................                         [100%]
48 passed in 0.16s
```

```bash
git diff --check
```

```text
# passed with no output
```
