"""Work at a Startup (YC) source + applicant.

Uses the saved YC session (secrets/yc_state.json). Two functions:
  scrape(): intern+eng jobs -> tracker as source 'waas'
  apply_waas(url, note): sends the WaaS application ("connect") with a short
    note drafted by Claude — WaaS applications message founders directly.
Applications are approval-gated like everything else: postings flow through the
normal tailor->email->approve cycle; on 'ready' the submitter calls apply_waas.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "tracker.db"
STATE = ROOT / "secrets" / "yc_state.json"
MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# YC rules (David 2026-08-17): interns AND full-time both OK for YC startups
# (full-time at a YC startup = acceptable alternative to internships);
# NEVER apply to P26-batch companies (too early). Batch filter is applied at
# ingest below.
LIST_URLS = [
    "https://www.workatastartup.com/companies?jobType=intern&sortBy=created_desc&role=eng",
    "https://www.workatastartup.com/companies?jobType=fulltime&sortBy=created_desc&role=eng",
]
LIST_URL = LIST_URLS[0]  # back-compat for callers that import LIST_URL
BLOCKED_BATCHES = ("P26",)


def _company_name(label: str, company_url: str = "") -> str:
    """Return the company name, never a directory CTA such as 'See all 8 jobs'."""
    for line in (label or "").splitlines():
        candidate = line.strip()
        if not candidate or re.fullmatch(r"see all\s+\d+\s+jobs?\s*[›>]?,?", candidate, re.I):
            continue
        return candidate[:60]
    parts = [unquote(part) for part in urlparse(company_url or "").path.split("/") if part]
    if len(parts) >= 2 and parts[-2].lower() == "companies":
        return parts[-1].replace("-", " ").replace("_", " ").title()[:60]
    return ""


def _send_button(page):
    """Select only the final modal Send control, never a background Apply button."""
    return page.get_by_role("button", name="Send", exact=True).last


def _browser(pw):
    # Local macOS runs keep this browser hidden off-screen. A VPS has no
    # desktop session, so allow deployment to opt into true headless mode.
    headless = os.environ.get("JOBHUNT_HEADLESS", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    args = ["--disable-blink-features=AutomationControlled"]
    if not headless:
        args.append("--window-position=-3200,-3200")
    b = pw.chromium.launch(headless=headless, args=args)
    ctx = b.new_context(storage_state=str(STATE), viewport={"width": 1280, "height": 1200})
    return b, ctx


def scrape(max_scroll: int = 6) -> int:
    from playwright.sync_api import sync_playwright
    rows = []
    with sync_playwright() as pw:
        b, ctx = _browser(pw)
        page = configure_page(ctx.new_page())
        cards = []
        for list_url in LIST_URLS:
            page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            for _ in range(max_scroll):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            cards += page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('a[href*="/jobs/"]').forEach(a => {
                    const t = a.innerText.trim();
                    if (!t || t === 'View job') return;
                    // company name: nearest heading above the job link
                    const card = a.closest('div[class*=company], div[class*=directory], div');
                    let comp = '';
                    let compUrl = '';
                    let el = card;
                    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
                        const h = el.querySelector('a[href*="/companies/"] span, a[href*="/companies/"]');
                        if (h && h.innerText.trim()) {
                            comp = h.innerText.trim().split('\\n')[0];
                            const link = h.closest('a') || h;
                            compUrl = link.href || '';
                            break;
                        }
                    }
                    out.push({url: a.href, title: t, company: comp.slice(0, 60), company_url: compUrl});
                });
                return out;
            }
            """)
        ctx.storage_state(path=str(STATE))
        b.close()
    seen = set()
    for c in cards:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        # /jobs/l/<role> links are directory listing pages, not postings
        # (Hive 2026-08-17: ingested one, adapter then crashed on it).
        if "/jobs/l/" in c["url"]:
            continue
        if re.search(r"mechatronics|electrical|hardware|mechanical", c["title"], re.I):
            continue
        # P26 = current early batch; David: never apply to P26 companies.
        if any(f"({b})" in (c.get("company") or "") for b in BLOCKED_BATCHES):
            continue
        c["company"] = _company_name(c.get("company", ""), c.get("company_url", ""))
        rows.append(c)
    conn = sqlite3.connect(DB)
    new = 0
    now = int(time.time())
    for r in rows:
        pid = f"waas:{r['url'].rsplit('/',1)[-1]}"
        if conn.execute("SELECT 1 FROM postings WHERE posting_id=?", (pid,)).fetchone():
            continue
        conn.execute("INSERT INTO postings (posting_id, source, company, title, locations, url, "
                     "sponsorship, citizenship_required, closed, first_seen, status) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, "waas", r["company"] or "YC startup", r["title"], "",
                      r["url"], "", 0, 0, now, "new"))
        new += 1
    conn.commit()
    conn.close()
    return new


NOTE_PROMPT = """Write a 90-120 word Work at a Startup application note to the founders for this job.
Candidate: David Cui — Brown CS+Econ, expected June 2028 (4.0), ex-founding CTO of Framewise Health (YC-backed,
patient video pipeline: Temporal/Python/Supabase/Claude), SWE intern at Freya (YC S25, real-time
LLM voice agents, p99 latency work), fraud-detection ML at Sotatek. USACO/AIME. Ships fast.

JOB POSTING:
{jd}

Rules: specific to THIS job's stack/problem, founder-to-founder tone (he ran a YC company),
no flattery, no "I'm excited". End without a signoff (WaaS shows the profile).
Return ONLY the note text."""


def _applied_badge_visible(page) -> bool:
    """WaaS shows a standalone 'Applied' line on the job page once the
    connect went through. This is the only reliable confirmation signal
    (2026-08-17 sweep: 12 sends marked unconfirmed were in fact applied)."""
    try:
        return any(line.strip().lower() == "applied"
                   for line in page.inner_text("body").splitlines())
    except Exception:
        return False


def apply_waas(url: str, slug: str, dry_run: bool = True) -> dict:
    from playwright.sync_api import sync_playwright
    result = {"ok": False, "submitted": False, "reason": ""}
    with sync_playwright() as pw:
        b, ctx = _browser(pw)
        page = configure_page(ctx.new_page())
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if _applied_badge_visible(page):
            result.update(ok=True, submitted=True,
                          reason="already applied (WaaS badge)")
            b.close()
            return result
        jd = page.inner_text("body")[:5000]
        # draft note
        body = json.dumps({"model": MODEL, "max_tokens": 500,
                           "messages": [{"role": "user", "content": NOTE_PROMPT.format(jd=jd)}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                     headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        note = "".join(bk.get("text", "") for bk in resp["content"] if bk.get("type") == "text").strip()
        result["note"] = note

        apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
        if not apply_btn.count():
            result["reason"] = "apply button not found (already applied?)"
            b.close()
            return result
        apply_btn.click(timeout=5000)
        page.wait_for_timeout(2000)
        ta = page.locator("textarea").first
        if ta.count():
            ta.fill(note)
        page.wait_for_timeout(500)
        send = _send_button(page)
        if not send.count() or not send.is_visible():
            result["reason"] = "final Send button not found"
            b.close()
            return result
        if dry_run:
            page.screenshot(path=str(ROOT / "out" / "screenshots" / f"{slug}_waas_filled.png"), full_page=True)
            result.update(ok=True, reason="dry run — note drafted, not sent")
            b.close()
            return result
        mark_submit_attempted()
        send.click(timeout=5000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(ROOT / "out" / "screenshots" / f"{slug}_waas_sent.png"), full_page=True)
        body = page.inner_text("body")
        if confirmation_observed(body, page.url):
            result.update(ok=True, submitted=True, reason="confirmed")
        else:
            # The modal often closes without a message; the job page badge is
            # authoritative. Reload and check it before declaring unconfirmed.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            if _applied_badge_visible(page):
                result.update(ok=True, submitted=True,
                              reason="confirmed (WaaS badge after send)")
            else:
                mark_unconfirmed(result)
        ctx.storage_state(path=str(STATE))
        b.close()
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "scrape":
        print("new postings:", scrape())
    else:
        print(apply_waas(sys.argv[1], "waas_test", dry_run=True))
