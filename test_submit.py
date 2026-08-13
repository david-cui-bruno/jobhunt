from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import submit
import submit_worker
from apply.jd import canonical_application_url, detect_ats


class SubmitSafetyTests(unittest.TestCase):
    def test_greenhouse_wrapper_urls_are_canonicalized_and_routed(self) -> None:
        for url, token in (
            ("https://www.hudsonrivertrading.com/careers/job/?gh_jid=8052083", "8052083"),
            ("https://www.trlm.com/apply/5207089007?gh_jid=5207089007", "5207089007"),
        ):
            with self.subTest(url=url):
                expected = f"https://boards.greenhouse.io/embed/job_app?token={token}"
                self.assertEqual(canonical_application_url(url), expected)
                self.assertEqual(detect_ats(url), "greenhouse")
                adapter, waas, detected, target = submit_worker._adapter("other", url)
                self.assertEqual(adapter.__name__, "apply_greenhouse")
                self.assertFalse(waas)
                self.assertEqual(detected, "greenhouse")
                self.assertEqual(target, expected)

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


if __name__ == "__main__":
    unittest.main()
