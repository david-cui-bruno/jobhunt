# Task 5 report: Manual Actions Sheet tab

## Summary
- Added `_collect_manual_actions(conn) -> list[list]` with the required columns:
  `Company`, `Role`, `ATS`, `Action`, `URL`, `Age`, `Attempt state`, `Prepared resume`, `Prepared screenshot`, `Latest reason`.
- Added `_ensure_sheet(sid, title) -> int` so `Manual Actions` is created idempotently for existing spreadsheets.
- Added `Manual Actions` to new spreadsheet creation and sync output, while preserving the existing Dashboard, Applications, and Pipeline tabs.
- The sheet output excludes submitted rows and pure engineering-debt rows. It emits booleans for prepared resume and screenshot availability, not local artifact paths or raw answers.

## TDD evidence
- RED: `python3 -m pytest -q tests/test_sheet_tracker.py -k manual`
  - Failed as expected before implementation with missing `_collect_manual_actions` and `_ensure_sheet` attributes.
  - Result: 3 failed, 1 deselected, 2 warnings.
- GREEN: `python3 -m pytest -q tests/test_sheet_tracker.py -k manual`
  - Result after implementation: 3 passed, 1 deselected, 2 warnings.

## Verification
- Focused: `python3 -m pytest -q tests/test_sheet_tracker.py`
  - Result: 4 passed, 2 warnings.
- Full: `python3 -m pytest -q`
  - Result: 474 passed, 2 warnings.
- Whitespace: `git diff --check`
  - Result: exit 0.

## Safety notes
- Did not access, stage, or modify `out/tracker.db`.
- Did not send notifications.
- Did not perform live Google Sheets writes. All Sheets behavior was covered by mocked `_api` tests and code inspection.

## Concerns / follow-up
- Existing Google Sheets formatting deletion for Application badge rules remains unchanged from prior behavior.
- The full suite still emits the pre-existing Python 3.9 google-auth deprecation warnings.
