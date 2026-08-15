"""Rippling ATS adapter (ats.rippling.com).

Quirks discovered by live recon:
  - the form lives in a CHILD frame (same origin, url without /apply)
  - inputs carry no type attribute and obfuscated, per-load-random names
  - labels are preceding-sibling text nodes, not <label for> associations
So: find the input-rich frame, harvest (input, preceding-label) pairs, map by
label text, fill; dropdown questions are typeahead inputs answered with text.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"

LABEL_JS = """() => {
    const out = [];
    [...document.querySelectorAll('input, textarea')]
      .filter(el => el.type === 'text' || el.tagName === 'TEXTAREA')
      .forEach((el, i) => {
        let label = '';
        let node = el;
        for (let d = 0; d < 6 && node && !label; d++, node = node.parentElement) {
            let sib = node.previousElementSibling;
            let hops = 0;
            while (sib && hops < 3) {
                const t = (sib.innerText || '').trim();
                if (t && t.length > 2 && t.length < 220) { label = t; break; }
                sib = sib.previousElementSibling; hops++;
            }
        }
        out.push({i, label: label.split('\\n')[0].trim(), val: String(el.value || '')});
    });
    return out;
}"""


def _answers(p: dict) -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"^first name", re.I), p["name"]["first"]),
        (re.compile(r"^last name", re.I), p["name"]["last"]),
        (re.compile(r"^email", re.I), p["email"]),
        (re.compile(r"^phone number", re.I), re.sub(r"[^0-9]", "", p["phone"])),
        (re.compile(r"^current company", re.I), "Brown University (student)"),
        (re.compile(r"^location", re.I), f"{p['location']['city']}, {p['location']['state']}"),
        (re.compile(r"linkedin", re.I), p["links"]["linkedin"]),
        (re.compile(r"website|portfolio|github", re.I), p["links"]["github"]),
        (re.compile(r"position are you applying", re.I), "__JOB_TITLE__"),
        (re.compile(r"earliest start date", re.I), "May 2027 (flexible)"),  # Summer 2027 internship start, NOT grad date
        (re.compile(r"legally eligible to work", re.I), "Yes"),
        (re.compile(r"visa sponsorship", re.I), "No"),
        (re.compile(r"how did you hear", re.I), "Company careers page"),
        (re.compile(r"salary|compensation|pay", re.I), "Open / market rate"),
    ]


def _text_inputs(frame):
    return frame.locator("input:not([type=file]):not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea")


def apply_rippling(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    apply_url = url if "/apply" in url else url.rstrip("/") + "/apply"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = configure_page(ctx.new_page())
        page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(8000)

        frame = next((f for f in page.frames if f.locator("input").count() > 5), None)
        if frame is None:
            result["reason"] = "application frame not found"
            _shot(page, slug, "fail_frame")
            browser.close()
            return result

        # resume first (may autofill)
        try:
            frame.locator("input[type=file]").first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(6000)
        except Exception as e:
            result["reason"] = f"resume upload failed: {e}"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

        # derive job title from page heading for the "position applying for" answer
        try:
            title = frame.locator("h1, h2").first.inner_text().strip()[:80]
        except Exception:
            title = "Software Engineer Intern"

        rules = _answers(p)
        for _pass in range(3):
            fields = frame.evaluate(LABEL_JS)
            inputs = _text_inputs(frame)
            for f in fields:
                if f["val"].strip():
                    continue
                for rx, val in rules:
                    if rx.search(f["label"] or ""):
                        v = title if val == "__JOB_TITLE__" else val
                        try:
                            el = inputs.nth(f["i"])
                            el.fill(v)
                            page.wait_for_timeout(300)
                            if rx.pattern.startswith("^location"):
                                page.wait_for_timeout(1300)
                                opt = frame.locator("[role=option]").first
                                if opt.count():
                                    opt.click()
                        except Exception:
                            pass
                        break
            page.wait_for_timeout(500)

        # force-overwrite email: resume parser autofills the school email
        fields = frame.evaluate(LABEL_JS)
        inputs = _text_inputs(frame)
        for f in fields:
            if re.match(r"^email", f["label"] or "", re.I) and p["email"] not in f["val"]:
                try:
                    el = inputs.nth(f["i"])
                    el.fill("")
                    el.fill(p["email"])
                    page.wait_for_timeout(300)
                except Exception:
                    pass

        # required-empty check within the frame
        fields = frame.evaluate(LABEL_JS)
        empty_required = [f["label"] for f in fields
                          if not f["val"].strip() and "*" in (f["label"] or "")]
        result["unanswered"] = empty_required
        _shot(page, slug, "filled")

        if empty_required:
            result["ok"] = True
            result["reason"] = f"needs answers: {empty_required[:6]}"
            browser.close()
            return result
        if dry_run:
            result["ok"] = True
            result["reason"] = "dry run — did not submit"
            browser.close()
            return result

        try:
            # Consent radio gates the Apply button (recon 2026-08-08: SpreeAI).
            # Radios have NO label association; find by nearby text via DOM walk.
            try:
                frame.evaluate("""() => {
                    const radios = [...document.querySelectorAll('input[type=radio]')];
                    for (const r of radios) {
                        let n = r.parentElement, txt = '';
                        for (let d = 0; d < 5 && n && !txt.trim(); d++, n = n.parentElement)
                            txt = (n.innerText || '').trim();
                        if (/yes.*consent/i.test(txt) && !/not consent/i.test(txt.split('\\n')[0])) {
                            if (!r.checked) r.click();
                            return txt.slice(0, 60);
                        }
                    }
                    return null;
                }""")
                page.wait_for_timeout(1200)
            except Exception:
                pass
            # The Apply/Submit button lives in the PARENT page's sticky header
            # (recon 2026-08-08: SpreeAI), not inside the application iframe.
            # Search frame then page; click the first VISIBLE, enabled match.
            clicked = False
            for scope in (frame, page):
                cands = scope.locator("button:has-text('Submit'), button:has-text('Apply')")
                for i in range(cands.count()):
                    b = cands.nth(i)
                    try:
                        if b.is_visible() and b.is_enabled():
                            mark_submit_attempted()
                            b.click(timeout=6000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                # Playwright click gets intercepted by an overlay on this board
                # (recon 2026-08-08); a synthetic DOM click dispatches fine.
                for scope in (frame, page):
                    mark_submit_attempted()
                    r = scope.evaluate("""() => {
                        const b = [...document.querySelectorAll('button')]
                            .find(x => /apply|submit/i.test(x.innerText || ''));
                        if (b) { b.click(); return true; }
                        return false;
                    }""")
                    if r:
                        clicked = True
                        break
            if not clicked:
                raise PWTimeout("no visible enabled Submit/Apply button")
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


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


if __name__ == "__main__":
    print(apply_rippling(sys.argv[1], Path(sys.argv[2]), "rip_test", dry_run=True))
