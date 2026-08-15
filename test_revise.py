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
                    status TEXT,
                    last_error TEXT
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
                INSERT INTO postings VALUES ('batch-only', 'Batch Co', 'Engineer', 'tailored', NULL);
                INSERT INTO emails VALUES (
                    'batch-only', NULL, NULL, '/tmp/resume.pdf', '/tmp/resume.tex', 1, 0
                );
                INSERT INTO postings VALUES ('direct', 'Direct Co', 'Engineer', 'tailored', NULL);
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
                    {"approved": [], "skipped": [], "manual_review": []},
                )

            get_thread.assert_called_once_with("gmail-thread")

    def test_revision_feedback_never_rewrites_a_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            tex = Path(tmp) / "resume.tex"
            tex.write_text("canonical resume")
            conn = sqlite3.connect(db)
            conn.executescript(
                f"""
                CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    status TEXT, last_error TEXT
                );
                CREATE TABLE emails (
                    posting_id TEXT PRIMARY KEY, thread_id TEXT, message_id TEXT,
                    resume_pdf TEXT, resume_tex TEXT, sent_at INTEGER,
                    revision INTEGER DEFAULT 0
                );
                CREATE TABLE sent_messages (message_id TEXT PRIMARY KEY);
                INSERT INTO postings VALUES ('p1', 'Acme', 'Engineer', 'tailored', NULL);
                INSERT INTO emails VALUES (
                    'p1', 'thread', 'sent', '{tmp}/resume.pdf', '{tex}', 1, 0
                );
                """
            )
            conn.commit()
            conn.close()

            thread = {"messages": [{"id": "reply", "internalDate": "2000"}]}
            with (
                mock.patch.object(revise, "DB", db),
                mock.patch.object(revise.mailer, "get_thread", return_value=thread),
                mock.patch.object(revise.mailer, "extract_plain", return_value="change the header"),
            ):
                self.assertEqual(
                    revise.poll_once(verbose=False),
                    {"approved": [], "skipped": [], "manual_review": ["Acme — Engineer"]},
                )

            conn = sqlite3.connect(db)
            status, error = conn.execute(
                "SELECT status,last_error FROM postings WHERE posting_id='p1'"
            ).fetchone()
            conn.close()
            self.assertEqual("manual", status)
            self.assertIn("canonical template", error)
            self.assertEqual("canonical resume", tex.read_text())


if __name__ == "__main__":
    unittest.main()
