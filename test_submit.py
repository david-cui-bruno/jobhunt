from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import submit
import submit_worker
import drip
from apply import submission_state
from apply import smartrecruiters
from apply.jd import canonical_application_url, detect_ats


def _write_quality(pdf: Path, posting_id: str, review_required: bool = False) -> None:
    pdf.with_suffix(".quality.json").write_text(json.dumps({
        "version": 1,
        "posting_id": posting_id,
        "source": "deterministic_grounded",
        "review_required": review_required,
        "structural_validation": "passed",
        "page_count": 1,
    }))


class SubmitSafetyTests(unittest.TestCase):
    def test_confirmation_requires_application_specific_evidence(self) -> None:
        accepted = (
            "Thank you for applying to Example Corp.",
            "We have received your application.",
            "Your application has been submitted successfully.",
        )
        for body in accepted:
            with self.subTest(body=body):
                self.assertTrue(submission_state.confirmation_observed(body))

        rejected = (
            "Success stories from our employees",
            "Thank you for visiting our careers page",
            "View your submitted jobs",
            "We received your cookie preferences",
        )
        for body in rejected:
            with self.subTest(body=body):
                self.assertFalse(submission_state.confirmation_observed(body))

        self.assertTrue(
            submission_state.confirmation_observed(
                "", "https://jobs.example.com/application-confirmation"
            )
        )

    def test_unconfirmed_click_is_quarantined(self) -> None:
        result = {"ok": True, "submitted": True, "retryable": True}
        submission_state.mark_unconfirmed(result)
        self.assertEqual("manual", result["outcome"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["retryable"])
        self.assertTrue(result["click_attempted"])
        self.assertTrue(result["submission_uncertain"])

    def test_worker_crash_after_submit_marker_is_never_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "submit.attempted"

            def crash_after_click(*_args, **_kwargs):
                submission_state.mark_submit_attempted()
                raise RuntimeError("browser vanished after click")

            payload = {
                "url": "https://example.com/job",
                "ats": "greenhouse",
                "slug": "example",
                "resume_pdf": str(Path(tmp) / "resume.pdf"),
                "dry_run": False,
            }
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    submit_worker,
                    "_adapter",
                    return_value=(crash_after_click, False, "greenhouse", payload["url"]),
                ),
                mock.patch.object(submit_worker.sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(submit_worker.sys, "stdout", stdout),
                mock.patch.dict(
                    os.environ,
                    {submission_state.MARKER_ENV: str(marker)},
                    clear=False,
                ),
            ):
                self.assertEqual(0, submit_worker.main())

            result = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual("manual", result["outcome"])
            self.assertFalse(result["retryable"])
            self.assertTrue(result["click_attempted"])
            self.assertTrue(result["submission_uncertain"])

    def test_worker_crash_before_submit_marker_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "submit.attempted"

            def crash_before_click(*_args, **_kwargs):
                raise RuntimeError("browser failed before form load")

            payload = {
                "url": "https://example.com/job",
                "ats": "greenhouse",
                "slug": "example",
                "resume_pdf": str(Path(tmp) / "resume.pdf"),
                "dry_run": False,
            }
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    submit_worker,
                    "_adapter",
                    return_value=(crash_before_click, False, "greenhouse", payload["url"]),
                ),
                mock.patch.object(submit_worker.sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(submit_worker.sys, "stdout", stdout),
                mock.patch.dict(
                    os.environ,
                    {submission_state.MARKER_ENV: str(marker)},
                    clear=False,
                ),
            ):
                self.assertEqual(0, submit_worker.main())

            result = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual("retryable_failure", result["outcome"])
            self.assertTrue(result["retryable"])
            self.assertFalse(result["click_attempted"])
            self.assertFalse(result["submission_uncertain"])

    def test_every_live_adapter_marks_submit_and_rejects_unconfirmed_success(self) -> None:
        adapter_paths = (
            Path("apply/greenhouse.py"),
            Path("apply/lever.py"),
            Path("apply/ashby.py"),
            Path("apply/workday.py"),
            Path("apply/smartrecruiters.py"),
            Path("apply/rippling.py"),
            Path("watcher/waas.py"),
        )
        for relative in adapter_paths:
            source = (Path(__file__).parent / relative).read_text()
            with self.subTest(adapter=str(relative)):
                self.assertIn("mark_submit_attempted", source)
                self.assertIn("confirmation_observed", source)
                self.assertIn("mark_unconfirmed", source)
                self.assertNotIn("submitted (no confirm text", source)

    def test_outbound_ledgers_are_never_overwritten(self) -> None:
        root = Path(__file__).parent
        sprint_source = (root / "sprint.py").read_text()
        email_source = (root / "email_apply.py").read_text()
        self.assertNotIn("INSERT OR REPLACE INTO applications", sprint_source)
        self.assertNotIn("INSERT OR REPLACE INTO applications", email_source)
        self.assertIn("_claim_submission", sprint_source)

    def test_smartrecruiters_expiry_and_captcha_are_not_upload_failures(self) -> None:
        self.assertEqual(
            {"outcome": "stale", "reason": "posting expired"},
            smartrecruiters._preflight_outcome("This job has expired"),
        )
        captcha = smartrecruiters._preflight_outcome("Active job", captcha_present=True)
        self.assertEqual("manual", captcha["outcome"])
        self.assertEqual(["SmartRecruiters CAPTCHA"], captcha["unanswered"])
        self.assertIn("job has expired", submit.DEAD_MARKERS)

    def test_greenhouse_wrapper_urls_are_canonicalized_and_routed(self) -> None:
        wrappers = (
            ("https://example.com/job?gh_jid=8052083", "hrt", "8052083"),
            ("https://example.com/apply?gh_jid=5207089007", "trillium", "5207089007"),
        )
        for url, board, token in wrappers:
            with self.subTest(url=url), mock.patch(
                "apply.jd._get",
                return_value=(
                    "<script src='https://boards.greenhouse.io/embed/job_board/"
                    f"js?for={board}'></script>"
                ),
            ), mock.patch(
                "jd._get",
                return_value=(
                    "<script src='https://boards.greenhouse.io/embed/job_board/"
                    f"js?for={board}'></script>"
                ),
            ):
                expected = (
                    "https://job-boards.greenhouse.io/embed/job_app"
                    f"?for={board}&token={token}"
                )
                self.assertEqual(canonical_application_url(url), expected)
                self.assertEqual(detect_ats(url), "greenhouse")
                adapter, waas, detected, target = submit_worker._adapter("other", url)
                self.assertEqual(adapter.__name__, "apply_greenhouse")
                self.assertFalse(waas)
                self.assertEqual(detected, "greenhouse")
                self.assertEqual(target, expected)

    def test_ashby_and_jane_street_wrappers_are_canonicalized(self) -> None:
        ashby_id = "807adafc-7842-4e05-90f3-9bc45dd39a13"
        self.assertEqual(
            f"https://jobs.ashbyhq.com/shopify/{ashby_id}",
            canonical_application_url(f"https://www.shopify.com/careers?ashby_jid={ashby_id}"),
        )
        self.assertEqual(
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=janestreet&token=8599644002",
            canonical_application_url(
                "https://www.janestreet.com/join-jane-street/position/8599644002/"
            ),
        )

    def test_terminal_http_status_marks_posting_dead(self) -> None:
        import urllib.error

        error = urllib.error.HTTPError("https://dead", 410, "gone", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertTrue(submit._posting_dead("https://dead.example/job"))

    def test_missing_ashby_board_marks_wrapper_posting_dead(self) -> None:
        import urllib.error

        job_id = "404bb82e-37f3-4a78-b0f3-12923a7c4856"
        error = urllib.error.HTTPError(
            "https://api.ashbyhq.com/posting-api/job-board/shopify",
            404,
            "not found",
            {},
            None,
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertTrue(
                submit._posting_dead(f"https://jobs.ashbyhq.com/shopify/{job_id}")
            )

    def test_unrecognized_careers_url_remains_manual(self) -> None:
        url = "https://www.oracle.com/careers/"
        self.assertEqual(canonical_application_url(url), url)
        adapter, waas, detected, target = submit_worker._adapter("other", url)
        self.assertIsNone(adapter)
        self.assertFalse(waas)
        self.assertEqual(detected, "other")
        self.assertEqual(target, url)

    def test_both_dry_run_spellings_are_safe(self) -> None:
        self.assertTrue(submit._dry_run_requested(["submit.py", "--dry"]))
        self.assertTrue(submit._dry_run_requested(["submit.py", "--dry-run"]))
        self.assertFalse(submit._dry_run_requested(["submit.py"]))

    def test_cli_dry_run_invokes_submitter_without_live_writes(self) -> None:
        with mock.patch.object(submit, "submit_ready", return_value=[]) as submit_ready:
            submit.main(["submit.py", "--dry-run"])

        submit_ready.assert_called_once_with(dry_run=True)

    def test_runtime_path_relocates_laptop_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relocated = root / "out" / "resumes" / "candidate.pdf"
            relocated.parent.mkdir(parents=True)
            relocated.write_bytes(b"pdf")

            stale = "/Users/old-user/jobhunt/out/resumes/candidate.pdf"
            self.assertEqual(submit._runtime_path(stale, root), relocated)

    def test_limited_dry_run_checks_one_posting_without_mutating_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            _write_quality(pdf, "two")
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY,
                    company TEXT,
                    title TEXT,
                    status TEXT,
                    url TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                INSERT INTO postings VALUES ('one', 'One', 'Engineer', 'ready', 'https://one');
                INSERT INTO postings VALUES ('two', 'Two', 'Engineer', 'ready', 'https://two');
                """
            )
            conn.executemany(
                "INSERT INTO emails VALUES (?, ?)",
                [("one", str(pdf)), ("two", str(pdf))],
            )
            conn.commit()
            conn.close()

            result = {
                "outcome": "failed",
                "ok": False,
                "submitted": False,
                "reason": "dry run",
            }
            with (
                mock.patch.object(submit, "DB", db),
                mock.patch.object(submit, "_user_is_gaming", return_value=False),
                mock.patch.object(submit, "_posting_dead", return_value=False),
                mock.patch.object(submit, "_isolated_adapter", return_value=result) as adapter,
            ):
                results = submit.submit_ready(limit=1, dry_run=True)

            self.assertEqual(len(results), 1)
            self.assertEqual(adapter.call_count, 1)
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute("SELECT status FROM postings ORDER BY posting_id").fetchall(),
                [("ready",), ("ready",)],
            )
            self.assertEqual(
                conn.execute("SELECT SUM(attempt_count) FROM postings").fetchone()[0], 0
            )
            conn.close()

    def test_ready_row_with_application_ledger_entry_is_never_resubmitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            _write_quality(pdf, "two")
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    status TEXT, url TEXT, outcome TEXT, last_attempt_at INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                INSERT INTO postings (posting_id,company,title,status,url)
                VALUES ('one','One','Engineer','ready','https://one');
                INSERT INTO applications VALUES
                ('one','resume.pdf','greenhouse',1,'submitted','');
                """
            )
            conn.execute("INSERT INTO emails VALUES ('one', ?)", (str(pdf),))
            conn.commit()
            conn.close()

            with (
                mock.patch.object(submit, "DB", db),
                mock.patch.object(submit, "_user_is_gaming", return_value=False),
                mock.patch.object(submit, "_isolated_adapter") as adapter,
            ):
                self.assertEqual([], submit.submit_ready(limit=1, dry_run=True))

            adapter.assert_not_called()

    def test_two_ready_rows_for_one_company_submit_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            # submit_ready orders newest rows first, so posting "two" owns the
            # artifact used before the company-level duplicate guard fires.
            _write_quality(pdf, "two")
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    status TEXT, url TEXT, outcome TEXT, last_attempt_at INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                INSERT INTO postings (posting_id,company,title,status,url) VALUES
                    ('one','Example','Engineer I','ready','https://one'),
                    ('two',' example ','Engineer II','ready','https://two');
                """
            )
            conn.executemany(
                "INSERT INTO emails VALUES (?,?)",
                [("one", str(pdf)), ("two", str(pdf))],
            )
            conn.commit()
            conn.close()

            result = {
                "outcome": "submitted",
                "ok": True,
                "submitted": True,
                "reason": "confirmed",
                "detected_ats": "greenhouse",
            }
            with (
                mock.patch.object(submit, "DB", db),
                mock.patch.object(submit, "_user_is_gaming", return_value=False),
                mock.patch.object(submit, "_posting_dead", return_value=False),
                mock.patch.object(submit, "_isolated_adapter", return_value=result) as adapter,
                mock.patch.object(submit.time, "sleep"),
            ):
                results = submit.submit_ready(limit=2)

            self.assertEqual(1, adapter.call_count)
            self.assertEqual(["submitted", "stale"], [r["outcome"] for r in results])
            conn = sqlite3.connect(db)
            self.assertEqual(1, conn.execute("SELECT count(*) FROM applications").fetchone()[0])
            self.assertEqual(
                [("filtered_out",), ("submitted",)],
                conn.execute("SELECT status FROM postings ORDER BY status").fetchall(),
            )
            conn.close()

    def test_review_required_resume_is_quarantined_before_adapter_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            _write_quality(pdf, "one", review_required=True)
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    status TEXT, url TEXT, outcome TEXT, last_attempt_at INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                INSERT INTO postings (posting_id,company,title,status,url)
                VALUES ('one','One','Engineer','ready','https://one');
                """
            )
            conn.execute("INSERT INTO emails VALUES ('one', ?)", (str(pdf),))
            conn.commit()
            conn.close()

            with (
                mock.patch.object(submit, "DB", db),
                mock.patch.object(submit, "_user_is_gaming", return_value=False),
                mock.patch.object(submit, "_isolated_adapter") as adapter,
            ):
                results = submit.submit_ready(limit=1)

            adapter.assert_not_called()
            self.assertEqual("manual", results[0]["outcome"])
            self.assertIn("flagged for human review", results[0]["reason"])
            conn = sqlite3.connect(db)
            self.assertEqual(
                ("manual", "manual", 0),
                conn.execute(
                    "SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'"
                ).fetchone(),
            )
            conn.close()

    def test_legacy_tailored_rows_require_current_quality_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.pdf"
            legacy = root / "legacy.pdf"
            valid.write_bytes(b"pdf")
            legacy.write_bytes(b"pdf")
            _write_quality(valid, "valid")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, status TEXT,
                    outcome TEXT, last_error TEXT, last_attempt_at INTEGER
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                INSERT INTO postings (posting_id,company,status) VALUES
                    ('valid','Verified','tailored'),
                    ('legacy','Legacy','tailored');
                """
            )
            conn.executemany(
                "INSERT INTO emails VALUES (?,?)",
                (("valid", str(valid)), ("legacy", str(legacy))),
            )

            self.assertEqual((1, 1), drip.promote_legacy_tailored(conn))
            self.assertEqual(
                [("legacy", "manual"), ("valid", "ready")],
                [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT posting_id,status FROM postings ORDER BY posting_id"
                    ).fetchall()
                ],
            )
            reason = conn.execute(
                "SELECT last_error FROM postings WHERE posting_id='legacy'"
            ).fetchone()[0]
            self.assertIn("metadata is missing", reason)
            conn.close()

    def test_retailor_publishes_immutably_after_state_recheck(self) -> None:
        source = (Path(__file__).parent / "retailor_one.py").read_text()
        self.assertIn("status IN ('manual','failed')", source)
        self.assertIn('conn.execute("BEGIN IMMEDIATE")', source)
        self.assertIn("os.replace(source, destination)", source)
        self.assertNotIn("shutil.copyfile", source)

    def test_sprint_claim_cannot_pass_an_existing_company_application(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE postings (
                posting_id TEXT PRIMARY KEY, company TEXT, status TEXT,
                last_attempt_at INTEGER
            );
            CREATE TABLE applications (posting_id TEXT PRIMARY KEY);
            INSERT INTO postings VALUES
                ('old','Example','submitted',1),
                ('new',' example ','sprinting',1);
            INSERT INTO applications VALUES ('old');
            """
        )
        self.assertEqual(
            "already_applied",
            submit._claim_submission(
                conn, "new", " example ", from_status="sprinting"
            ),
        )
        self.assertEqual(
            "sprinting",
            conn.execute(
                "SELECT status FROM postings WHERE posting_id='new'"
            ).fetchone()[0],
        )
        conn.close()

    def test_uncertain_worker_result_is_never_retried_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            _write_quality(pdf, "one")
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    status TEXT, url TEXT, outcome TEXT, last_attempt_at INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                INSERT INTO postings (posting_id,company,title,status,url)
                VALUES ('one','One','Engineer','ready','https://one');
                """
            )
            conn.execute("INSERT INTO emails VALUES ('one', ?)", (str(pdf),))
            conn.commit()
            conn.close()

            result = {
                "outcome": "retryable_failure",
                "ok": False,
                "submitted": False,
                "submission_uncertain": True,
                "reason": "worker timed out after a possible click",
            }
            with (
                mock.patch.object(submit, "DB", db),
                mock.patch.object(submit, "_user_is_gaming", return_value=False),
                mock.patch.object(submit, "_posting_dead", return_value=False),
                mock.patch.object(submit, "_isolated_adapter", return_value=result),
                mock.patch.object(submit, "_send_notice", return_value=True),
                mock.patch.object(submit.time, "sleep"),
            ):
                results = submit.submit_ready(limit=1)

            self.assertEqual(results[0]["outcome"], "retryable_failure")
            conn = sqlite3.connect(db)
            self.assertEqual(
                ("manual", "retryable_failure", 1),
                conn.execute(
                    "SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'"
                ).fetchone(),
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
