"""Lever application adapter. Reuses the qa.py Q&A engine.

Lever posting URLs: jobs.lever.co/<org>/<uuid> ; apply form at /apply.
Form is classic HTML (inputs + selects), far simpler than Greenhouse.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


def apply_lever(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    apply_url = url.rstrip("/")
    if not apply_url.endswith("/apply"):
        apply_url += "/apply"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--window-position=-3200,-3200"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = ctx.new_page()
        page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)

        # resume first: Lever parses it and autofills some fields
        try:
            page.locator("input[type=file]").first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(4000)
        except Exception as e:
            result["reason"] = f"resume upload failed: {e}"
            browser.close()
            return result

        # core fields (fill after parse so we overwrite bad autofill)
        for sel, val in [
            ("input[name='name']", f"{p['name']['first']} {p['name']['last']}"),
            ("input[name='email']", p["email"]),
            ("input[name='phone']", p["phone"]),
            ("input[name='org']", "Brown University"),
            ("input[name='urls[LinkedIn]']", p["links"]["linkedin"]),
            ("input[name='urls[GitHub]']", p["links"]["github"]),
        ]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.fill(val)
            except Exception:
                pass

        # location typeahead
        try:
            loc = page.locator("input[name='location']").first
            if loc.count() and loc.is_visible():
                loc.fill(f"{p['location']['city']}, {p['location']['state']}")
                page.wait_for_timeout(1200)
                opt = page.locator(".dropdown-container li, ul[role=listbox] li").first
                if opt.count():
                    opt.click()
        except Exception:
            pass

        # custom questions + EEO via Q&A engine (multi-pass like greenhouse)
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
            page.wait_for_timeout(600)
        result["qa_filled"] = sorted(set(filled_qa))
        result["qa_failed"] = failed_qa

        required_empty = page.evaluate("""
            () => {
                const bad = [];
                document.querySelectorAll('.application-question.required, [required], [aria-required="true"]').forEach(el => {
                    const root = el.classList?.contains('application-question') ? el : null;
                    const inp = root ? root.querySelector('input, select, textarea') : el;
                    if (!inp || inp.type === 'file' || inp.getAttribute('aria-hidden') === 'true') return;
                    if (inp.type === 'checkbox' || inp.type === 'radio') {
                        if ([...document.querySelectorAll('input')].filter(x => x.name === inp.name).some(x => x.checked)) return;
                    } else if ((inp.value || '').trim()) return;
                    const lbl = (root?.querySelector('.application-label')?.innerText
                        || inp.labels?.[0]?.innerText || inp.name || inp.id || 'unknown');
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
            page.locator("button#btn-submit, button:has-text('Submit application')").first.click(timeout=5000)
            page.wait_for_timeout(5000)
            _shot(page, slug, "submitted")
            body = page.inner_text("body").lower()
            ok_text = any(w in body for w in ("thank", "received", "application has been"))
            result.update(ok=True, submitted=True,
                          reason="confirmed" if ok_text else "submitted (no confirm text)")
        except PWTimeout:
            result["reason"] = "submit button not found"
        browser.close()
    return result


if __name__ == "__main__":
    print(apply_lever(sys.argv[1], Path(sys.argv[2]), "lever_test", dry_run=True))
