"""Workable application adapter. Reuses the qa.py Q&A engine.

Workable posting URLs: apply.workable.com/<account>/j/<CODE>/ ; form at /apply.
The form is a single-page React app with stable name attributes for core
fields (firstname, lastname, email, phone) and CA_*/QA_* names for custom
questions, which the generic qa.py extractor handles via labels.

Liveness is checked first through the public API
(apply.workable.com/api/v2/accounts/<account>/jobs/<CODE>), which returns
404 for removed postings and {"state": ...} for live ones.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"

_URL_RE = re.compile(r"apply\.workable\.com/([^/]+)/j/([A-Za-z0-9]+)", re.I)


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


def _posting_state(url: str) -> str | None:
    """Return the API state for a posting, or None when the API is unreachable."""
    m = _URL_RE.search(url)
    if not m:
        return None
    account, code = m.groups()
    api = f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{code}"
    req = urllib.request.Request(
        api, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        data = json.load(urllib.request.urlopen(req, timeout=20))
        return str(data.get("state") or "unknown")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "removed"
        return None
    except Exception:
        return None


def _apply_url(url: str) -> str:
    m = _URL_RE.search(url)
    if not m:
        return url
    account, code = m.groups()
    return f"https://apply.workable.com/{account}/j/{code}/apply/"


def apply_workable(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}

    state = _posting_state(url)
    if state == "removed":
        result.update(outcome="stale", reason="posting removed (Workable API 404)")
        return result

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = configure_page(ctx.new_page())
        page.goto(_apply_url(url), wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        body = page.inner_text("body").lower()
        if "this position is no longer available" in body or "job not found" in body:
            result.update(outcome="stale", reason="posting no longer available")
            browser.close()
            return result

        # cookie banner
        for sel in ["button[data-ui='cookie-consent-accept']", "button:has-text('Accept')"]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.click(timeout=2000)
                    break
            except Exception:
                pass

        # resume first: Workable parses it and may autofill fields
        try:
            page.locator("input[type=file]").first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(6000)  # parse settle
        except Exception as e:
            result["reason"] = f"resume upload failed: {e}"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

        # core fields (after parse so we overwrite bad autofill)
        for sel, val in [
            ("input[name='firstname']", p["name"]["first"]),
            ("input[name='lastname']", p["name"]["last"]),
            ("input[name='email']", p["email"]),
            ("input[name='phone']", p["phone"]),
            ("input[name='address']", p["location"]["city"]),
            ("input[name='city']", p["location"]["city"]),
        ]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.fill(val)
            except Exception:
                pass

        # custom questions + EEO via Q&A engine (multi-pass like lever/greenhouse)
        answers = None
        filled_qa, failed_qa = [], []
        for qa_pass in range(3):
            controls = page.evaluate(qa.EXTRACT_JS)
            if answers is None:
                qa.harvest_select_options(page, controls)
                answers = qa.get_answers(controls, context={"slug": slug, "url": url})
            live = {c["id"] or c["name"] for c in controls if not c["value"] and not c.get("chosen")}
            todo = [a for a in answers if a["id_or_name"] in live] if qa_pass else answers
            if qa_pass and not todo:
                break
            f, x = qa.fill_answers(page, controls, todo)
            filled_qa += f
            failed_qa = x
            page.wait_for_timeout(600)
        result["qa_filled"] = sorted(set(filled_qa))
        result["qa_failed"] = failed_qa

        required_empty = page.evaluate("""
            () => {
                const bad = [];
                document.querySelectorAll('[required], [aria-required="true"]').forEach(inp => {
                    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(inp.tagName)) return;
                    if (inp.type === 'file' || inp.type === 'hidden') return;
                    if (inp.getAttribute('aria-hidden') === 'true') return;
                    if (inp.type === 'checkbox' || inp.type === 'radio') {
                        if ([...document.querySelectorAll('input')].filter(x => x.name === inp.name).some(x => x.checked)) return;
                    } else if ((inp.value || '').trim()) return;
                    const lbl = (inp.labels?.[0]?.innerText
                        || inp.closest('[class*=styles--field], [role=group], fieldset')?.querySelector('label, legend, strong')?.innerText
                        || inp.getAttribute('aria-label') || inp.name || 'unknown');
                    bad.push(lbl.replace(/\\s+/g, ' ').trim().slice(0, 80));
                });
                return [...new Set(bad)];
            }
        """)
        result["unanswered"] = required_empty
        _shot(page, slug, "filled")

        if required_empty:
            result["ok"] = True
            result["reason"] = f"needs answers: {required_empty[:6]}"
            browser.close()
            return result
        if dry_run:
            result["ok"] = True
            result["reason"] = "dry run — did not submit"
            browser.close()
            return result

        try:
            mark_submit_attempted()
            page.locator("button[data-ui='apply-button'], button:has-text('Submit application')").first.click(timeout=5000)
            page.wait_for_timeout(6000)
            _shot(page, slug, "submitted")
            body = page.inner_text("body").lower()
            if confirmation_observed(body, page.url):
                result.update(ok=True, submitted=True, reason="confirmed")
            else:
                mark_unconfirmed(result)
        except PWTimeout:
            mark_unconfirmed(result, "submit click timed out; verify possible prior submission")
        browser.close()
    return result


if __name__ == "__main__":
    print(apply_workable(sys.argv[1], Path(sys.argv[2]), "workable_test", dry_run=True))
