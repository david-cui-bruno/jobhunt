"""Stealth launch: the fingerprint tells must be gone (David 2026-08-20).

Ashby's spam filter and SmartRecruiters' DataDome gate read the headless
browser's fingerprint. The single biggest tell was navigator.webdriver===true;
this locks in that the shared launch hides it and presents a coherent, honest
identity. (It does not claim to defeat IP-level or behavioral blocks, which a
live submit showed it does not.)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apply")]

import stealth  # noqa: E402


class StealthConfigTests(unittest.TestCase):
    def test_ua_matches_installed_chromium_major(self):
        # a UA/engine mismatch is itself a bot tell; keep them in lockstep.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            major = browser.version.split(".")[0]
            browser.close()
        self.assertIn(f"Chrome/{major}.0.0.0", stealth.UA)

    def test_automation_flag_disabled(self):
        self.assertIn("--disable-blink-features=AutomationControlled", stealth.LAUNCH_ARGS)


class StealthRuntimeTests(unittest.TestCase):
    """Actually launch and read the fingerprint back out of a page."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._pw_ctx = sync_playwright()
        pw = cls._pw_ctx.__enter__()
        cls.browser, cls.ctx = stealth.launch_stealth_context(pw)
        cls.page = cls.ctx.new_page()
        cls.page.set_content("<html><body>x</body></html>")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw_ctx.__exit__(None, None, None)

    def test_webdriver_hidden(self):
        # the headline fix: navigator.webdriver was true, must now be undefined
        self.assertIsNone(self.page.evaluate("() => navigator.webdriver"))

    def test_identity_is_coherent(self):
        got = self.page.evaluate(
            "() => ({ua: navigator.userAgent, langs: navigator.languages, "
            "plugins: navigator.plugins.length, chrome: !!window.chrome})"
        )
        self.assertEqual(got["ua"], stealth.UA)
        self.assertIn("en-US", got["langs"])
        self.assertGreater(got["plugins"], 0)
        self.assertTrue(got["chrome"])

    def test_timezone_pinned(self):
        tz = self.page.evaluate(
            "() => Intl.DateTimeFormat().resolvedOptions().timeZone")
        self.assertEqual(tz, "America/New_York")


if __name__ == "__main__":
    unittest.main()
