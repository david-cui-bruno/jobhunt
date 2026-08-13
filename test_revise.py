from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import revise


class RevisePollingTests(unittest.TestCase):
    def test_runtime_path_relocates_laptop_resume_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relocated = root / "out" / "resumes" / "candidate.tex"
            relocated.parent.mkdir(parents=True)
            relocated.write_text("resume")

            stale = "/Users/old-user/jobhunt/out/resumes/candidate.tex"
            self.assertEqual(revise._runtime_path(stale, root), relocated)

    def test_poll_once_ignores_email_rows_without_gmail_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY,
                    company TEXT,
                    title TEXT,
                    status TEXT
                );
                CREATE TABLE emails (
                    posting_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    message_id TEXT,
                    resume_pdf TEXT,
                    resume_tex TEXT,
                    sent_at INTEGER,
                    revision INTEGER DEFAULT 0
                );
                CREATE TABLE sent_messages (message_id TEXT PRIMARY KEY);
                INSERT INTO postings VALUES ('batch-only', 'Batch Co', 'Engineer', 'tailored');
                INSERT INTO emails VALUES (
                    'batch-only', NULL, NULL, '/tmp/resume.pdf', '/tmp/resume.tex', 1, 0
                );
                INSERT INTO postings VALUES ('direct', 'Direct Co', 'Engineer', 'tailored');
                INSERT INTO emails VALUES (
                    'direct', 'gmail-thread', 'gmail-message', '/tmp/resume.pdf',
                    '/tmp/resume.tex', 1, 0
                );
                """
            )
            conn.commit()
            conn.close()

            with (
                mock.patch.object(revise, "DB", db),
                mock.patch.object(
                    revise.mailer, "get_thread", return_value={"messages": []}
                ) as get_thread,
            ):
                self.assertEqual(
                    revise.poll_once(verbose=False),
                    {"approved": [], "skipped": [], "revised": []},
                )

            get_thread.assert_called_once_with("gmail-thread")


if __name__ == "__main__":
    unittest.main()
