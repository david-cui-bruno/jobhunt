from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import submit
import submit_worker
from apply import smartrecruiters
from apply.jd import canonical_application_url, detect_ats


class SubmitSafetyTests(unittest.TestCase):
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

    def test_uncertain_worker_result_is_never_retried_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
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
