# Task 6 report: per-ATS metrics and local rollout docs

## Commit

- SHA: `3668f7558b9ee006a4aa4a755db47bf1cf9df53d`
- Subject: `Report per-ATS submission health`

## Implementation changed

- Created `submission/metrics.py`.
  - Added `attempt_metrics(conn, *, since)`.
  - Counts are grouped with SQL over `submission_attempts` by ATS and outcome.
  - Durations are read without answer-bank or screenshot content and p50 and p95 are computed in Python.
  - Unknown, blank, or unsupported ATS values normalize to `other`.
  - Confirmed attempts count `confirmation_observed=1` and `outcome='submitted'`.
  - Returned rows include attempts, confirmed count, confirmation rate, outcome counts, p50, and p95.
  - Added `queue_metrics(conn)` using `submission.lanes.classify_url` as the shared lane classifier for current `queued`, `ready`, and `manual` postings.
- Updated `digest.py`.
  - `collect()` now adds `attempt_metrics`, `queue_metrics`, and Ashby breaker state.
  - Digest composition adds a compact `submission health` section.
  - The section includes only top failing ATS lines in `ats confirmed/attempts confirmed` format, lane queue depths, and Ashby breaker status only when paused.
  - No per-item email notifications were added or restored.
  - The phone body remains capped by `SHORT_LIMIT`.
- Updated `tests/test_digest_phone.py`.
  - Added compact digest composition coverage for failing ATSs, confirmed ratios, lane queue depths, Ashby breaker paused line, and `SHORT_LIMIT`.
- Created `tests/test_submission_metrics.py`.
  - Covers grouping by ATS and outcome, confirmed ratios, unknown ATS normalization, p50 and p95 duration, and queue grouping by shared lane classification.

## Documentation changed

- Updated `README.md`.
  - Describes the resident local `submit_daemon.py` dispatcher polling every 30 seconds.
  - Documents ATS lanes, Ashby breaker policy, canonical posting dedupe, and the `submission_attempts` attempt ledger.
  - Updates reporting to Telegram plus email and names the new operational digest content.
  - Removes stale submit cadence claims that described a 65 minute global submit loop.
- Updated `docs/system-design.html`.
  - Replaces the old submit loop description with the resident 30 second ATS lane dispatcher.
  - Adds ATS lane, canonical dedupe, and attempt ledger descriptions.
  - Adds digest operational metrics content.
  - Updates the timers table to show submit as resident.

## TDD RED evidence

Command:

```bash
cd /Users/davidcui824/jobhunt/.worktrees/hybrid-dispatcher
python3 -m pytest -q tests/test_submission_metrics.py tests/test_digest_phone.py
```

Expected RED observed before implementation:

```text
ERROR tests/test_submission_metrics.py
ModuleNotFoundError: No module named 'submission.metrics'
```

This confirmed the new metrics interface was missing before implementation.

## TDD GREEN evidence

Command after implementation:

```bash
python3 -m pytest -q tests/test_submission_metrics.py tests/test_digest_phone.py
```

Output:

```text
17 passed, 2 warnings in 0.13s
```

After changing `attempt_metrics` to use SQL grouping for counts, the focused tests were rerun:

```text
17 passed, 2 warnings in 0.13s
```

## Full suite output

Final full-suite command:

```bash
python3 -m pytest -q
```

Final output:

```text
317 passed, 2 warnings in 1.45s
```

Warnings were the existing Python 3.9 end-of-life warnings from `google-auth`.

## Daemon dry-run setup and nonmutation proof

I did not run the daemon against the source checkout database. I created an ignored local fixture copy under `out/dryrun-task6/repo`, which is ignored by the repository's existing `out/` ignore rule. I copied the worktree there, created a minimal fixture `out/tracker.db`, and ran the exact daemon command inside that ignored copy:

```bash
cd /Users/davidcui824/jobhunt/.worktrees/hybrid-dispatcher
rm -rf out/dryrun-task6
mkdir -p out/dryrun-task6/repo
rsync -a --exclude '.git' --exclude 'out' --exclude '.venv' ./ out/dryrun-task6/repo/
cd out/dryrun-task6/repo
# create fixture out/tracker.db with one ready Greenhouse row and a quality-checked fixture PDF
python3 submit_daemon.py --once --dry-run
```

Observed output included the selected dry-run row:

```text
2026-08-22 04:25:52,285 INFO root: dispatch result: {'company': 'Fixture Co', 'ats': 'unknown', 'outcome': 'stale', 'reason': 'liveness check marked posting stale', 'posting_id': 'fixture-greenhouse', 'lane': 'direct'}
```

Before and after status proof from the fixture database:

```text
before=[('fixture-greenhouse', 'ready', '')]
after=[('fixture-greenhouse', 'ready', '')]
attempt_rows 0
```

The fixture posting status and outcome were unchanged. No external click was performed. The ignored fixture copy was removed before the final full-suite run so pytest would not collect duplicate tests from it.

After validation, `out/tracker.db` in the isolated worktree appeared modified, likely from suite-level database setup. I immediately restored it with:

```bash
git restore -- out/tracker.db
git status --short
```

Final tracked status was clean before writing this report. `out/tracker.db` was not staged or committed.

## Documentation checks

Commands:

```bash
python3 - <<'PY'
from html.parser import HTMLParser
from pathlib import Path
HTMLParser().feed(Path('docs/system-design.html').read_text())
for path in ['README.md','docs/system-design.html']:
    text=Path(path).read_text()
    stale=[s for s in ['every 65 min','65 min | submit','iMessage copy','hourly global','hourly-global'] if s in text]
    if stale:
        raise SystemExit(f'{path} stale refs: {stale}')
print('docs check passed: system-design HTML parses and stale rollout claims absent')
PY
python3 - <<'PY'
import subprocess
bad=[]
diff=subprocess.check_output(['git','diff','--','README.md','docs/system-design.html'], text=True)
for line in diff.splitlines():
    if line.startswith('+') and ('—' in line or '–' in line):
        bad.append(line)
if bad:
    raise SystemExit('unicode dash in added prose:\n'+'\n'.join(bad))
print('new prose dash check passed')
PY
```

Output:

```text
docs check passed: system-design HTML parses and stale rollout claims absent
new prose dash check passed
```

## Self-review

- Verified that metrics source is `submission_attempts` and not posting statuses.
- Verified queue grouping uses `submission.lanes.classify_url`, so shared lane classification owns queue grouping.
- Verified no answer-bank values or screenshot contents are read for metrics.
- Verified digest text is compact and still returns `body[:SHORT_LIMIT]` for the phone copy.
- Verified Ashby breaker line appears only when the cooldown file has a future timestamp.
- Verified no per-item notification emails were introduced.
- Verified docs describe the resident 30 second dispatcher, ATS lanes, canonical posting dedupe, and attempt ledger.
- Verified docs no longer describe submit as a 65 minute global submit loop.
- Verified no launchd install, reload, bootstrap, bootout, kickstart, or print commands were run.

## Concerns and follow-ups

- The dry-run fixture under `out/dryrun-task6/repo` caused an intermediate full-suite collection mismatch because pytest recursively found duplicate tests inside the ignored copy. I removed the fixture copy and reran the full suite successfully. Future dry-run fixtures should live outside the repository tree or be excluded explicitly from pytest collection.
- `submit_daemon.py --once --dry-run` still performs the liveness check before adapter execution. It did not click or mutate the fixture posting, but the fixture row was reported as stale because the dummy Greenhouse URL was not live.
- The isolated worktree `out/tracker.db` appeared modified after validation and was restored before completion. It was never staged or committed.

## Live service confirmation

No live launchd or service rollout commands ran. I did not install, reload, bootstrap, bootout, kickstart, or print the live `com.jobhunt.submit` launchd job. External rollout remains deferred for reviewed integration.

## Fix Round 1

### RED evidence

Command:

```bash
cd /Users/davidcui824/jobhunt/.worktrees/hybrid-dispatcher
python3 -m pytest -q tests/test_submission_metrics.py
```

Output before the implementation fix:

```text
.F..                                                                     [100%]
=================================== FAILURES ===================================
______ test_attempt_metrics_requires_observed_confirmation_for_submitted _______
E       assert 2 == 1
FAILED tests/test_submission_metrics.py::test_attempt_metrics_requires_observed_confirmation_for_submitted
1 failed, 3 passed in 0.05s
```

The failing regression proved that an attempt with `outcome='submitted'` and `confirmation_observed=0` was incorrectly counted as confirmed.

### Change

- Added `test_attempt_metrics_requires_observed_confirmation_for_submitted` in `tests/test_submission_metrics.py`.
- Changed the confirmed aggregate in `submission/metrics.py` from `confirmation_observed=1 OR outcome='submitted'` to `confirmation_observed=1 AND outcome='submitted'`.
- Preserved existing outcome counts, confirmation rate shape, duration metrics, queue metrics, and digest behavior.
- Did not touch `out/tracker.db`.

### Covering commands and outputs

Command:

```bash
python3 -m pytest -q tests/test_submission_metrics.py tests/test_digest_phone.py
```

Output:

```text
..................                                                       [100%]
=============================== warnings summary ===============================
../../../Library/Python/3.9/lib/python/site-packages/google/oauth2/__init__.py:40
  /Users/davidcui824/Library/Python/3.9/lib/python/site-packages/google/oauth2/__init__.py:40: FutureWarning: You are using a Python version 3.9 past its end of life. Google will update google-auth with critical bug fixes on a best-effort basis, but not with any other fixes or features. Please upgrade your Python version, and then update google-auth.
    warnings.warn(eol_message.format("3.9"), FutureWarning)

../../../Library/Python/3.9/lib/python/site-packages/google/auth/__init__.py:54
  /Users/davidcui824/Library/Python/3.9/lib/python/site-packages/google/auth/__init__.py:54: FutureWarning: You are using a Python version 3.9 past its end of life. Google will update google-auth with critical bug fixes on a best-effort basis, but not with any other fixes or features. Please upgrade your Python version, and then update google-auth.
    warnings.warn(eol_message.format("3.9"), FutureWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
18 passed, 2 warnings in 0.09s
```
