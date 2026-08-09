"""Greenhouse application adapter.

Fills the standard Greenhouse application form (job-boards.greenhouse.io) with profile data
and the tailored resume. Screenshot saved at every stage; on any uncertainty we bail without
submitting and mark the posting for manual review.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import qa
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _fill_if_present(page: Page, selector: str, value: str) -> bool:
    el = page.locator(selector).first
    try:
        if el.count() and el.is_visible():
            el.fill(value)
            return True
    except Exception:
        pass
    return False


def _shot(page: Page, slug: str, stage: str):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


def apply_greenhouse(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    """Returns {ok, submitted, reason, screenshot}."""
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = configure_page(ctx.new_page())
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        # cookie banners
        for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
            try:
                if page.locator(sel).first.is_visible():
                    page.locator(sel).first.click(timeout=2000)
                    break
            except Exception:
                pass

        # Newer boards have an Apply tab/button that reveals the form
        for sel in ["a:has-text('Apply')", "button:has-text('Apply')"]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        filled = {}
        filled["first"] = _fill_if_present(page, "input#first_name", p["name"]["first"])
        filled["last"] = _fill_if_present(page, "input#last_name", p["name"]["last"])
        filled["email"] = _fill_if_present(page, "input#email", p["email"])
        filled["phone"] = _fill_if_present(page, "input#phone", p["phone"])

        if not (filled["first"] and filled["email"]):
            result["reason"] = "core fields not found (nonstandard board)"
            _shot(page, slug, "fail_fields")
            browser.close()
            return result

        # resume upload
        try:
            file_inputs = page.locator("input[type=file]")
            file_inputs.first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(3000)  # parse/upload settle
        except Exception as e:
            result["reason"] = f"resume upload failed: {e}"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

        # LinkedIn/website if asked
        _fill_if_present(page, "input[autocomplete='custom-question-linkedin-profile']",
                         p["links"]["linkedin"])
        for label in ["LinkedIn", "Website", "GitHub", "Portfolio"]:
            try:
                el = page.get_by_label(re.compile(label, re.I)).first
                if el.count() and el.is_visible() and el.input_value() == "":
                    val = p["links"]["linkedin"] if label == "LinkedIn" else p["links"]["github"]
                    el.fill(val)
            except Exception:
                pass

        # School/degree combos, EEO selects, and custom questions -> Claude Q&A engine.
        # React-select fills are flaky; run up to 3 passes, retrying only still-empty controls.
        answers = None
        filled_qa, failed_qa = [], []
        for qa_pass in range(3):
            controls = page.evaluate(qa.EXTRACT_JS)
            if answers is None:
                qa.harvest_select_options(page, controls)
                answers = qa.get_answers(controls)
            live = {c["id"] or c["name"] for c in controls if not c["value"] and not c.get("chosen")}
            todo = [a for a in answers if a["id_or_name"] in live] if qa_pass else answers
            if qa_pass and not todo:
                break
            f, x = qa.fill_answers(page, controls, todo)
            filled_qa += f
            failed_qa = x
            page.wait_for_timeout(800)
        result["qa_filled"] = sorted(set(filled_qa))
        result["qa_failed"] = failed_qa
        page.wait_for_timeout(1000)

        # NEW Greenhouse flow (recon 2026-08-09, The Nuclear Company): the email
        # verification code boxes render INLINE on the form pre-submit. Fetch the
        # code from Gmail and type it before the required-fields scan.
        sec = page.locator("input[id*=security-input], input[name*=security-input]")
        if sec.count():
            code = _fetch_gh_code()
            if code:
                n = sec.count()
                if n >= len(code):
                    for i, ch in enumerate(code[:n]):
                        sec.nth(i).fill(ch)
                        page.wait_for_timeout(120)
                else:
                    sec.first.fill(code)
                page.wait_for_timeout(600)
            else:
                result["reason"] = "inline verification code email not found"
                _shot(page, slug, "fail_code")
                browser.close()
                return result

        required_empty = page.evaluate("""
            () => {
                const bad = [];
                document.querySelectorAll('[aria-required="true"], [required]').forEach(el => {
                    // react-select hidden decoy inputs: not user-facing
                    if (el.getAttribute('aria-hidden') === 'true') return;
                    // inline verification code boxes: handled by the code-fetch flow
                    if (/security|verification/i.test(el.id || el.name || '')) return;
                    // file upload group: satisfied when a filename chip is rendered
                    if (el.classList?.contains('file-upload')) {
                        if (el.innerText.includes('.pdf') || el.querySelector('[class*=chip], [class*=file-name]')) return;
                        bad.push('Resume upload');
                        return;
                    }
                    // required FIELDSET = checkbox/radio group: ok if any inner box checked
                    if (el.tagName === 'FIELDSET') {
                        if ([...el.querySelectorAll('input')].some(x => x.checked)) return;
                        const lbl = el.querySelector('legend, .label')?.innerText || el.getAttribute('name') || 'group';
                        bad.push(lbl.slice(0, 80));
                        return;
                    }
                    if ((el.value || '').trim()) return;
                    // react-select puts the chosen value in a sibling span, input stays empty
                    const shell = (el.parentElement || el).closest('.select-shell, .select__container, [class*=select-shell]');
                    const chosen = shell?.querySelector('.select__single-value, [class*=singleValue], [class*=single-value]');
                    if (chosen && chosen.innerText.trim()) return;
                    // multi-selects render chips instead of a single-value span
                    const chips = shell?.querySelector('.select__multi-value, [class*=multiValue], [class*=multi-value]');
                    if (chips) return;
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        if ([...document.querySelectorAll('input')].filter(x => x.name === el.name).some(x => x.checked)) return;
                    }
                    const lbl = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || el.name || el.id;
                    bad.push(lbl?.slice(0, 80) || 'unknown');
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

        # submit
        try:
            page.locator("button:has-text('Submit application'), input[type=submit]").first.click(timeout=5000)
            page.wait_for_timeout(4000)
            # email verification gate: Greenhouse sends a 6-char code to the applicant email
            body = page.inner_text("body").lower()
            if "verification code" in body or "security code" in body:
                code = _fetch_gh_code()
                if not code:
                    result["reason"] = "verification code email not found"
                    _shot(page, slug, "fail_code")
                    browser.close()
                    return result
                boxes = page.locator("input[autocomplete='one-time-code'], input[maxlength='1']")
                if boxes.count() >= 6:
                    for i, ch in enumerate(code[:6]):
                        boxes.nth(i).fill(ch)
                else:
                    page.locator("input[name*=code i], input[id*=code i], input[type=text]:below(:text('code'))").first.fill(code)
                page.wait_for_timeout(800)
                _shot(page, slug, "code_entered")
                sub = page.locator("button:has-text('Submit application'), button:has-text('Verify'), input[type=submit]").first
                if sub.count():
                    sub.click(timeout=5000)
                page.wait_for_timeout(4000)
            _shot(page, slug, "submitted")
            body = page.inner_text("body").lower()
            if "thank" in body or "received" in body or "submitted" in body:
                result.update(ok=True, submitted=True, reason="confirmed")
            else:
                result.update(ok=True, submitted=True, reason="submitted (no confirm text found)")
        except PWTimeout:
            result["reason"] = "submit button not found"
        browser.close()
    return result


def _fetch_gh_code(timeout_s: int = 240) -> str | None:
    """Poll Gmail for the newest Greenhouse verification code."""
    import re as _re
    import time as _time
    sys.path.insert(0, str(ROOT / "notify"))
    import mailer
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            data = mailer._call("/messages?q=newer_than:1h%20(verification%20OR%20security%20OR%20greenhouse)&maxResults=8")
            for m in data.get("messages", [])[:5]:
                full = mailer._call(f"/messages/{m['id']}?format=full")
                text = mailer.extract_plain(full) or full.get("snippet", "")
                mm = _re.search(r"\b([A-Z0-9]{6})\b", text)
                if mm and int(full.get("internalDate", 0)) / 1000 > _time.time() - 300:
                    return mm.group(1)
        except Exception:
            pass
        _time.sleep(6)
    return None


if __name__ == "__main__":
    url = sys.argv[1]
    pdf = Path(sys.argv[2])
    print(apply_greenhouse(url, pdf, "manual_test", dry_run=True))
