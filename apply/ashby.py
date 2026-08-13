"""Ashby application adapter. Reuses the qa.py Q&A engine.

jobs.ashbyhq.com/<org>/<uuid> -> application tab has a React form.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


def apply_ashby(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    # normalize: strip query params (?embed=... breaks the standalone form), ensure /application
    base = url.split("?")[0].rstrip("/")
    apply_url = base if base.endswith("/application") else base + "/application"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600})
        page = configure_page(ctx.new_page())
        page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        # resume upload first: Ashby autofills from it too
        try:
            page.locator("input[type=file]").first.set_input_files(str(resume_pdf))
            page.wait_for_timeout(4500)
        except Exception as e:
            result["reason"] = f"resume upload failed: {e}"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

        # basics via label matching (Ashby ids are generated)
        for label, val in [
            ("Name", f"{p['name']['first']} {p['name']['last']}"),
            ("Email", p["email"]),
            ("Phone", p["phone"]),
            ("LinkedIn", p["links"]["linkedin"]),
            ("GitHub", p["links"]["github"]),
        ]:
            try:
                el = page.get_by_label(label, exact=False).first
                if el.count() and el.is_visible() and not el.input_value():
                    el.fill(val)
            except Exception:
                pass

        answers = None
        filled_qa, failed_qa = [], []
        for qa_pass in range(3):
            controls = page.evaluate(qa.EXTRACT_JS)
            if answers is None:
                qa.harvest_select_options(page, controls)
                answers = qa.get_answers(controls, context={"slug": slug, "url": apply_url})
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
                document.querySelectorAll('[aria-required="true"], [required]').forEach(el => {
                    if (el.getAttribute('aria-hidden') === 'true' || el.type === 'file') return;
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        if ([...document.querySelectorAll('input')].filter(x => x.name === el.name).some(x => x.checked)) return;
                    } else if ((el.value || '').trim()) return;
                    const wrap = el.closest('[class*=_fieldEntry], [class*=field]');
                    const chosen = wrap?.querySelector('[class*=singleValue], [class*=single-value], [class*=_option]');
                    if (chosen && chosen.innerText.trim()) return;
                    const lbl = el.labels?.[0]?.innerText || wrap?.querySelector('label')?.innerText || el.id || 'unknown';
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
            page.locator("button:has-text('Submit application'), button:has-text('Submit Application')").first.click(timeout=5000)
            page.wait_for_timeout(5000)
            _shot(page, slug, "submitted")
            body = page.inner_text("body").lower()
            ok_text = any(w in body for w in ("thank", "received", "success"))
            result.update(ok=True, submitted=True,
                          reason="confirmed" if ok_text else "submitted (no confirm text)")
        except PWTimeout:
            result["reason"] = "submit button not found"
        browser.close()
    return result


if __name__ == "__main__":
    print(apply_ashby(sys.argv[1], Path(sys.argv[2]), "ashby_test", dry_run=True))
