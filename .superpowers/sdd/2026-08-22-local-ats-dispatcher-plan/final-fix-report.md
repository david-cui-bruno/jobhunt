# Final fix report

Worktree: `/Users/davidcui824/jobhunt/.worktrees/hybrid-dispatcher`
Base reviewed: `a3b8fb4c0450dc08f0e2f970a0793a6cd22eff17`
Fix commit: `231a81405f61c6f441fe86d50d706f881dfd3987` - `Fix dispatcher final review findings`

## Numbered findings mapped to fixes

1. `scripts/migrate_launchd_secrets.py` direct invocation failed because script-path execution did not put the repo root on `sys.path`.
   - Code: `scripts/migrate_launchd_secrets.py` now prepends the repository root before importing `runtime_secrets`.
   - Test: `tests/test_runtime_secrets.py::test_migration_documented_direct_cli_runs_without_exposing_values` runs the documented subprocess CLI shape and checks mode 600 plus no secret value in output.
   - RED: focused RED run failed with `ModuleNotFoundError: No module named 'runtime_secrets'`.
   - GREEN: focused GREEN run passed in the 13-test regression set.

2. `submission/database.py` enabled foreign keys on the shared runtime writer.
   - Code: `submission/database.py` removed `PRAGMA foreign_keys=ON` while preserving WAL and busy timeout.
   - Test: `tests/test_submission_attempts.py::test_connect_tracker_does_not_enable_foreign_key_enforcement` asserts `PRAGMA foreign_keys` remains `0`.
   - RED: focused RED run failed with `assert 1 == 0`.
   - GREEN: focused GREEN run passed in the 13-test regression set.

3. WaaS and HN/email routing was quarantined as unsupported, and WaaS submit worker routing was unreachable behind the unsupported check.
   - Code: `submission/lanes.py` adds explicit nonautomatic `email` and `waas` lane policies for HN, mailto, and Work at a Startup URLs. `submit_worker.py` checks WaaS before unsupported routing and requires `JOBHUNT_WAAS=1` before returning `apply_waas`.
   - Tests: `tests/test_submission_lanes.py::test_hn_and_waas_sources_are_not_classified_as_unsupported`, `tests/test_submission_lanes.py::test_quarantine_unsupported_moves_unknown_queued_row_to_manual`, and `tests/test_submission_lanes.py::test_waas_submit_worker_branch_requires_opt_in`.
   - RED: focused RED run failed because HN classified as `unsupported`, quarantine count was `3` instead of `1`, and the WaaS worker returned detected `other` instead of `waas_paused`.
   - GREEN: focused GREEN run passed in the 13-test regression set.

4. `email_apply.py` still blocked all future email applications for a company after any prior application at that company.
   - Code: `email_apply.py` removed company-wide suppression and uses `posting_already_applied` plus `_active_posting_claimed` canonical posting identity checks before and during the durable send claim.
   - Tests: `test_approval_policy.py::AutonomousApprovalPolicyTests::test_existing_distinct_company_role_does_not_block_email_application` and `test_existing_same_canonical_posting_blocks_email_application`.
   - RED: focused RED run failed because the distinct same-company HN role returned `[]` instead of sending.
   - GREEN: focused GREEN run passed in the 13-test regression set.

5. Dispatcher dry-run could race a resident daemon because it used a ready row without claiming it.
   - Code: `submission/executor.py` rechecks the current posting status before any dry-run quality, liveness, or adapter work and skips if the row is no longer `ready`.
   - Test: `tests/test_submission_executor.py::test_executor_dry_run_rechecks_ready_status_before_adapter` mutates the row to `submitted` before dry-run execution and asserts no adapter launch.
   - RED: focused RED run failed because the adapter was called.
   - GREEN: focused GREEN run passed in the 13-test regression set.

6. Executor resume quality quarantine bypassed normalized outcome persistence.
   - Code: `submission/executor.py` now uses the shared `_mark_outcome` path for quality gate failures, keeping status, outcome, last error, and attempt count consistent while still creating no attempt row because no adapter launched.
   - Tests: `tests/test_submission_executor.py::test_executor_quality_gate_uses_normalized_outcome_without_attempt`; existing `test_submit.py::SubmitSafetyTests::test_review_required_resume_is_quarantined_before_adapter_launch` updated to the normalized attempt count.
   - RED: focused RED run failed with attempt count `0` where normalized persistence expected `1`.
   - GREEN: focused GREEN run passed in the 13-test regression set and the full suite passed.

7. Claim-lost after a confirmed submit could finish the attempt as `submitted` even while the posting moved manual.
   - Code: `submission/executor.py` normalizes the local result and final attempt outcome to `manual` with no observed confirmation when `_mark_outcome` loses the claim after an adapter-reported submit.
   - Test: `tests/test_submission_executor.py::test_executor_claim_lost_after_confirmed_submit_finishes_attempt_as_manual`.
   - RED: focused RED run failed because the attempt outcome was `submitted`.
   - GREEN: focused GREEN run passed in the 13-test regression set.

8. Claimed row reload failure left rows stuck as `submitting` until stale recovery.
   - Code: `submission/dispatcher.py` adds `_release_missing_claim` and immediately moves the claimed row to manual with normalized fields when the joined claimed row cannot be reloaded. No attempt ledger is created.
   - Test: `tests/test_submission_dispatcher.py::test_claimed_row_missing_is_released_without_attempt_or_uncertainty`.
   - RED: focused RED run failed because the result stayed `skipped` and the row remained claimed.
   - GREEN: focused GREEN run passed in the 13-test regression set.

9. Documentation and deployment references still described stale global-loop or company-freeze behavior and dangling `jobhunt@submit.timer` inventory.
   - Code/docs: `README.md` and `docs/system-design.html` now describe canonical posting dedupe rather than company-wide freezing. `deploy/aws/verify-instance.sh`, `deploy/aws/stage-and-bootstrap.sh`, `deploy/VPS.md`, and `deploy/aws/README.md` now reference `jobhunt-submit.service` and the five remaining timers, not the deleted submit timer.
   - Tests: `test_aws_deploy.py::AwsDeploymentTests::test_obsolete_submit_timer_is_not_referenced` and `test_architecture_docs_use_canonical_posting_not_company_freeze`.
   - RED: focused RED run failed because `jobhunt@submit.timer` and stale company-freeze text were present.
   - GREEN: focused GREEN run passed in the 13-test regression set. Additional content checks found zero deployment matches for `jobhunt@submit.timer`, zero runtime helper matches for `foreign_keys=ON`, and zero architecture-doc matches for the stale company-freeze phrases.

## Validation evidence

- Focused RED for newly added regression tests:
  - `python3 -m pytest -q tests/test_runtime_secrets.py::test_migration_documented_direct_cli_runs_without_exposing_values tests/test_submission_attempts.py::test_connect_tracker_does_not_enable_foreign_key_enforcement tests/test_submission_lanes.py::test_hn_and_waas_sources_are_not_classified_as_unsupported tests/test_submission_lanes.py::test_quarantine_unsupported_moves_unknown_queued_row_to_manual test_approval_policy.py::AutonomousApprovalPolicyTests::test_existing_distinct_company_role_does_not_block_email_application test_approval_policy.py::AutonomousApprovalPolicyTests::test_existing_same_canonical_posting_blocks_email_application tests/test_submission_executor.py::test_executor_quality_gate_uses_normalized_outcome_without_attempt tests/test_submission_executor.py::test_executor_claim_lost_after_confirmed_submit_finishes_attempt_as_manual tests/test_submission_executor.py::test_executor_dry_run_rechecks_ready_status_before_adapter tests/test_submission_dispatcher.py::test_claimed_row_missing_is_released_without_attempt_or_uncertainty test_aws_deploy.py::AwsDeploymentTests::test_obsolete_submit_timer_is_not_referenced test_aws_deploy.py::AwsDeploymentTests::test_architecture_docs_use_canonical_posting_not_company_freeze` failed as expected with 11 failures and 1 pass.
  - `python3 -m pytest -q tests/test_submission_lanes.py::test_waas_submit_worker_branch_requires_opt_in` failed as expected.
- Focused GREEN for regression tests:
  - Same focused set plus `test_waas_submit_worker_branch_requires_opt_in`: `13 passed, 2 warnings in 0.20s`.
- Focused file suite:
  - `python3 -m pytest -q tests/test_runtime_secrets.py tests/test_submission_attempts.py tests/test_submission_lanes.py test_approval_policy.py tests/test_submission_dispatcher.py tests/test_submission_executor.py test_aws_deploy.py`: `68 passed, 2 warnings in 0.56s`.
- Full suite:
  - First full run exposed one expected-old-test assertion in `test_submit.py` after normalized outcome accounting: `1 failed, 344 passed, 2 warnings`.
  - Final full run: `345 passed, 2 warnings in 1.55s`.
- Launchd plist validation:
  - `find launchd -name '*.plist' -print0 | xargs -0 plutil -lint`: all six launchd plists returned `OK`.
- Diff and content checks:
  - `git diff --check`: passed with no output.
  - `git diff --name-only -- out/tracker.db out/tracker.db-shm out/tracker.db-wal`: no output.
  - Content checks: no matches in `submission` for `foreign_keys=ON`; no matches in `deploy` for `jobhunt@submit.timer`; no matches in README/system-design for required stale company-freeze phrases.

## Files changed in fix commit

- `README.md`
- `deploy/VPS.md`
- `deploy/aws/README.md`
- `deploy/aws/stage-and-bootstrap.sh`
- `deploy/aws/verify-instance.sh`
- `docs/system-design.html`
- `email_apply.py`
- `scripts/migrate_launchd_secrets.py`
- `submission/database.py`
- `submission/dispatcher.py`
- `submission/executor.py`
- `submission/lanes.py`
- `submit_worker.py`
- `test_approval_policy.py`
- `test_aws_deploy.py`
- `test_submit.py`
- `tests/test_runtime_secrets.py`
- `tests/test_submission_attempts.py`
- `tests/test_submission_dispatcher.py`
- `tests/test_submission_executor.py`
- `tests/test_submission_lanes.py`

## Side-effect and safety notes

- No live service control, provider rotation, Ashby canary, browser submission, AWS deployment, installed launchd plist read, or real runtime env migration command was run.
- No secret values were printed or copied. Migration CLI tests used synthetic fake values only.
- `out/tracker.db` and sidecars were not modified.
- Concern: final tests emit Python 3.9 Google auth end-of-life warnings from installed dependencies. They are not related to this fix wave.
