"""Unit tests for apply/workday_recovery.py.

Covers: generated-password complexity, reset-link/security-code extraction
from Gmail message payloads, the once-per-day recovery guard, and the
login-URL derivation.
"""
from __future__ import annotations

import base64
import re
import sqlite3
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apply"))

import workday_recovery as recovery  # noqa: E402


def _gmail_message(body_html: str, sender: str = "coreandmain@otp.workday.com",
                   subject: str = "Reset your password for your candidate account") -> dict:
    data = base64.urlsafe_b64encode(body_html.encode()).decode().rstrip("=")
    return {
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {},
            "parts": [{"mimeType": "text/html", "body": {"data": data}}],
        }
    }


class PasswordGenerationTests(unittest.TestCase):
    def test_meets_workday_complexity_rules(self):
        for _ in range(50):
            pw = recovery.generate_password()
            self.assertEqual(20, len(pw))
            self.assertTrue(any(c in string.ascii_uppercase for c in pw), pw)
            self.assertTrue(any(c in string.ascii_lowercase for c in pw), pw)
            self.assertTrue(any(c in string.digits for c in pw), pw)
            self.assertTrue(
                any(c in recovery.PASSWORD_SYMBOLS for c in pw), pw)
            # Only characters every tenant accepts (no quoting hazards).
            allowed = set(string.ascii_letters + string.digits
                          + recovery.PASSWORD_SYMBOLS)
            self.assertTrue(set(pw) <= allowed, pw)

    def test_passwords_are_unique_per_call(self):
        self.assertEqual(30, len({recovery.generate_password() for _ in range(30)}))

    def test_short_request_still_covers_all_groups(self):
        pw = recovery.generate_password(4)
        self.assertEqual(4, len(pw))
        self.assertTrue(any(c in string.ascii_uppercase for c in pw), pw)
        self.assertTrue(any(c in recovery.PASSWORD_SYMBOLS for c in pw), pw)


class ResetEmailParsingTests(unittest.TestCase):
    RESET_URL = ("https://coreandmain.wd1.myworkdayjobs.com/coreandmain/"
                 "passwordreset/yv8moiv2n5ijsv4nvalw1sj4byyjhe171efd674c")

    def test_extracts_reset_link_from_observed_email_shape(self):
        # Mirrors the real coreandmain email captured 2026-08-18.
        body = ("<body><div>Click this link to reset your password<br>"
                f"{self.RESET_URL}</div><div>If you didn&#39;t request this"
                "</div></body>")
        message = _gmail_message(body)
        self.assertEqual(
            self.RESET_URL,
            recovery.reset_url_from_message(
                message, "coreandmain.wd1.myworkdayjobs.com"),
        )

    def test_rejects_link_for_another_tenant_host(self):
        body = f"<a href='{self.RESET_URL}'>Reset</a>"
        self.assertIsNone(
            recovery.reset_url_from_message(
                _gmail_message(body), "nvidia.wd5.myworkdayjobs.com"))

    def test_ignores_non_reset_links_on_same_host(self):
        body = ("<a href='https://coreandmain.wd1.myworkdayjobs.com/"
                "coreandmain/job/x'>job</a>")
        self.assertIsNone(
            recovery.reset_url_from_message(
                _gmail_message(body), "coreandmain.wd1.myworkdayjobs.com"))

    def test_strips_trailing_punctuation_from_href(self):
        body = f"reset here: {self.RESET_URL}."
        self.assertEqual(
            self.RESET_URL,
            recovery.reset_url_from_message(
                _gmail_message(body), "coreandmain.wd1.myworkdayjobs.com"),
        )

    def test_security_code_variant(self):
        message = _gmail_message(
            "<p>Your security code is <b>483920</b>. It expires soon.</p>")
        self.assertEqual("483920",
                         recovery.security_code_from_message(message))

    def test_no_security_code_in_plain_text(self):
        message = _gmail_message("<p>Thanks for applying to Core and Main</p>")
        self.assertIsNone(recovery.security_code_from_message(message))


class DailyGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.addCleanup(Path(self.tmp.name).unlink)
        patcher = mock.patch.object(recovery, "DB", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_attempt_per_tenant_per_day(self):
        self.assertTrue(recovery.claim_daily_recovery("nvidia", day="2026-08-18"))
        self.assertFalse(recovery.claim_daily_recovery("nvidia", day="2026-08-18"))
        # A different tenant and a different day are both unaffected.
        self.assertTrue(recovery.claim_daily_recovery("amgen", day="2026-08-18"))
        self.assertTrue(recovery.claim_daily_recovery("nvidia", day="2026-08-19"))

    def test_outcome_is_recorded_on_todays_row(self):
        recovery.claim_daily_recovery("nvidia", day="2026-08-18")
        recovery.record_recovery_outcome("nvidia", "ok", day="2026-08-18")
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute(
            "SELECT outcome FROM wd_recovery_attempts WHERE tenant='nvidia' "
            "AND day='2026-08-18'").fetchone()
        conn.close()
        self.assertEqual("ok", row[0])

    def test_store_password_updates_before_submit_and_keeps_created_at(self):
        conn = sqlite3.connect(self.tmp.name)
        conn.execute(
            "CREATE TABLE wd_accounts (tenant TEXT PRIMARY KEY, email TEXT, "
            "password TEXT, created_at INTEGER)")
        conn.execute("INSERT INTO wd_accounts VALUES (?,?,?,?)",
                     ("nvidia", "davidcui824@gmail.com", "old", 1787042107))
        conn.commit()
        conn.close()
        recovery.store_password("nvidia", "davidcui824@gmail.com", "NewPw!123x")
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute(
            "SELECT password, created_at FROM wd_accounts WHERE tenant='nvidia'"
        ).fetchone()
        conn.close()
        self.assertEqual(("NewPw!123x", 1787042107), tuple(row))


class LoginUrlTests(unittest.TestCase):
    def test_plain_site(self):
        self.assertEqual(
            "https://coreandmain.wd1.myworkdayjobs.com/coreandmain/login",
            recovery.login_url_from(
                "https://coreandmain.wd1.myworkdayjobs.com/coreandmain/job/"
                "Sacramento/intern_R1234/apply/autofillWithResume"),
        )

    def test_locale_prefixed_site(self):
        self.assertEqual(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareers/login",
            recovery.login_url_from(
                "https://nvidia.wd5.myworkdayjobs.com/en-US/"
                "NVIDIAExternalCareers/job/US/Software-Intern_JR123"),
        )


if __name__ == "__main__":
    unittest.main()
