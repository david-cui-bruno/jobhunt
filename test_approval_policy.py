from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import batch
import batch_approve
import email_apply


class AutonomousApprovalPolicyTests(unittest.TestCase):
    def test_batch_queues_tailored_resume_without_sending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "tracker.db"
            pdf = root / "resume.pdf"
            pdf.write_bytes(b"pdf")
            conn = sqlite3.connect(db)
            conn.execute(
                """CREATE TABLE postings (
                    posting_id TEXT PRIMARY KEY, company TEXT, title TEXT,
                    locations TEXT, url TEXT, first_seen INTEGER, status TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO postings VALUES (?,?,?,?,?,?,?)",
                ("one", "Example", "Engineer", "Remote", "https://example", 1, "queued"),
            )
            conn.commit()
            conn.close()

            with (
                mock.patch.object(batch, "DB", db),
                mock.patch.object(batch, "fetch_jd", return_value="job"),
                mock.patch.object(batch, "tailor", return_value=pdf),
            ):
                self.assertEqual(batch.run_batch(limit=1), ["Example — Engineer"])

            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute("SELECT status FROM postings WHERE posting_id='one'").fetchone()[0],
                "ready",
            )
            self.assertEqual(
                conn.execute("SELECT thread_id,message_id FROM emails").fetchone(),
                (None, None),
            )
            conn.close()

    def test_batch_approval_sender_is_disabled(self) -> None:
        with mock.patch.object(batch_approve.mailer, "send") as send:
            self.assertEqual(batch_approve.send_batch(), 0)
        send.assert_not_called()

    def test_email_application_sends_to_company_without_approval_request(self) -> None:
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
                    url TEXT, status TEXT
                );
                CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
                CREATE TABLE applications (
                    posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
                    submitted_at INTEGER, confirmation TEXT, notes TEXT
                );
                CREATE TABLE sent_messages (message_id TEXT PRIMARY KEY);
                INSERT INTO postings VALUES (
                    'one', 'Example', 'Engineer',
                    'https://news.ycombinator.com/item?id=1', 'ready'
                );
                """
            )
            conn.execute("INSERT INTO emails VALUES (?,?)", ("one", str(pdf)))
            conn.commit()
            conn.close()

            draft = {"to": "jobs@example.com", "subject": "Application", "body": "Hello"}
            with (
                mock.patch.object(email_apply, "DB", db),
                mock.patch.object(email_apply, "fetch_hn_text", return_value="email jobs@example.com"),
                mock.patch.object(email_apply, "_claude", return_value=draft),
                mock.patch.object(
                    email_apply, "_send_application", return_value={"id": "sent-message"}
                ) as send_company,
                mock.patch.object(email_apply.mailer, "send") as send_approval,
            ):
                self.assertEqual(email_apply.compose_ready_email_postings(), ["Example"])

            send_company.assert_called_once_with(
                "jobs@example.com", "Application", "Hello", pdf
            )
            send_approval.assert_not_called()
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute("SELECT status FROM postings WHERE posting_id='one'").fetchone()[0],
                "submitted",
            )
            self.assertEqual(conn.execute("SELECT status FROM email_apps").fetchone()[0], "sent")
            self.assertEqual(conn.execute("SELECT ats FROM applications").fetchone()[0], "email")
            conn.close()

    def test_drip_has_no_approval_pollers_or_approval_sender(self) -> None:
        source = (Path(__file__).resolve().parent / "drip.py").read_text()
        self.assertNotIn("batch_approve", source)
        self.assertNotIn("import revise", source)
        self.assertNotIn("Reply: suggestions", source)


if __name__ == "__main__":
    unittest.main()
