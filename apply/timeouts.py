"""Shared per-posting Playwright timeout configuration.

The submitter also enforces a process-level deadline. This page-level timeout keeps
individual locator and navigation calls from consuming that entire budget.
"""
from __future__ import annotations

import os


DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 30_000


def configure_page(page):
    """Apply the bounded timeout used by one posting's browser session."""
    raw = os.environ.get("JOBHUNT_PLAYWRIGHT_TIMEOUT_MS", str(DEFAULT_PLAYWRIGHT_TIMEOUT_MS))
    try:
        timeout_ms = max(1_000, int(raw))
    except ValueError:
        timeout_ms = DEFAULT_PLAYWRIGHT_TIMEOUT_MS
    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(timeout_ms)
    return page
