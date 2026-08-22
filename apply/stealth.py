"""Compatibility stealth browser launch for non-Ashby ATS adapters.

This module preserves the generic hardened Chromium launcher used by existing
adapters that still expect a fresh `(browser, context)` pair. It masks common
Playwright automation tells for those compatibility paths, but it is not the
Ashby recovery backend and does not claim to solve Ashby's spam rejection path.

Ashby uses `apply/ashby_browser.py` instead: a dedicated persistent installed
Chrome profile that preserves the browser's real user agent, plugins, GPU,
languages, timezone, and hardware values. Do not route Ashby through this
launcher.
"""
from __future__ import annotations

import os

# A real macOS Chrome UA whose major version MATCHES the bundled Chromium
# (145 as of 2026-08-20). A UA/engine mismatch is itself a fingerprint tell,
# so this must track the installed version: check `pw.chromium.launch().version`
# after a Playwright upgrade.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

# Runs in every new document before page scripts: erase the automation tells
# DataDome / Ashby fingerprinting reads. Purely subtractive (hide webdriver,
# present normal plugins/languages) — no identity is invented.
_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(i => ({name: 'Plugin ' + i, filename: 'p' + i}))
});
window.chrome = window.chrome || {runtime: {}, app: {}, csi: () => {}, loadTimes: () => {}};
const _query = window.navigator.permissions && window.navigator.permissions.query;
if (_query) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _query(p)
  );
}
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
try {
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return gp.call(this, p);
  };
} catch (e) {}
"""


def launch_stealth_context(pw, *, viewport=None, locale="en-US"):
    """Launch a hardened headless Chromium and return (browser, context).

    Adapters should replace their `pw.chromium.launch(...)` + `new_context(...)`
    pair with this and keep using the returned context exactly as before.
    """
    headless = os.environ.get("JOBHUNT_HEADFUL") != "1"  # escape hatch for debugging
    browser = pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)
    context = browser.new_context(
        viewport=viewport or {"width": 1280, "height": 1600},
        user_agent=UA,
        locale=locale,
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(_STEALTH_JS)
    return browser, context
