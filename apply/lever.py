"""Lever application adapter. Reuses the qa.py Q&A engine.

Lever posting URLs: jobs.lever.co/<org>/<uuid> ; apply form at /apply.
Form is classic HTML (inputs + selects), far simpler than Greenhouse.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from artifacts import safe_screenshot
import qa
import stealth
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _lever_location_required_empty(page) -> list[str]:
    """Validate Lever's real location selection state, not the visible typeahead text."""
    return page.evaluate(r"""
        () => {
            const loc = document.querySelector('input#location-input[name="location"][required], input[name="location"][required]');
            if (!loc || loc.type === 'hidden' || loc.offsetParent === null) return [];
            const selected = document.querySelector('input#selected-location[name="selectedLocation"], input[name="selectedLocation"]');
            if ((selected?.value || '').trim()) return [];
            const lbl = (loc.closest('.application-question')?.querySelector('.application-label')?.innerText
                || loc.labels?.[0]?.innerText || 'Current location ✱');
            return [lbl.replace(/\s+/g, ' ').trim().slice(0, 80) || 'Current location ✱'];
        }
    """)


def _lever_generic_required_empty(page) -> list[str]:
    return page.evaluate(r"""
        () => {
            const bad = [];
            document.querySelectorAll('.application-question.required, [required], [aria-required="true"]').forEach(el => {
                const root = el.classList?.contains('application-question') ? el : null;
                const inp = root ? root.querySelector('input, select, textarea') : el;
                if (!inp || inp.type === 'file' || inp.getAttribute('aria-hidden') === 'true') return;
                if (inp.type === 'checkbox' || inp.type === 'radio') {
                    if ([...document.querySelectorAll('input')].filter(x => x.name === inp.name).some(x => x.checked)) return;
                } else if ((inp.value || '').trim()) return;
                const lbl = (el.closest('.application-question')?.querySelector('.application-label')?.innerText
                    || root?.querySelector('.application-label')?.innerText
                    || inp.labels?.[0]?.innerText || inp.name || inp.id || 'unknown');
                bad.push(lbl.replace(/\s+/g, ' ').trim().slice(0, 80));
            });
            return [...new Set(bad)];
        }
    """)


def _lever_required_empty(page) -> list[str]:
    return list(dict.fromkeys(_lever_generic_required_empty(page) + _lever_location_required_empty(page)))


def _lever_required_reason(required_empty: list[str]) -> str:
    if len(required_empty) == 1 and "current location" in required_empty[0].lower():
        return "needs manual Lever location selection: location typeahead is hCaptcha-gated"
    return f"needs answers: {required_empty[:6]}"


def lever_captcha_present(page) -> bool:
    selectors = (
        "iframe[src*='hcaptcha.com']",
        "iframe[title*='hcaptcha' i]",
        "textarea[name='h-captcha-response']",
        "[data-sitekey][class*='h-captcha']",
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            if locator.nth(index).is_visible():
                return True
    return False


def _shot(page, slug, stage):
    shot = safe_screenshot(page, slug, stage, root=SHOTS)
    return shot


def _record_filled_screenshot(result: dict, page, slug: str) -> None:
    shot = safe_screenshot(page, slug, "filled", root=SHOTS)
    if shot:
        result.setdefault("artifact_refs", {})["filled_form_screenshot"] = shot


def apply_lever(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    apply_url = url.rstrip("/")
    if not apply_url.endswith("/apply"):
        apply_url += "/apply"
    with sync_playwright() as pw:
        browser, ctx = stealth.launch_stealth_context(pw)
        page = configure_page(ctx.new_page())
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
        answered_keys: set = set()
        filled_qa, failed_qa = [], []
        for qa_pass in range(3):
            controls = page.evaluate(qa.EXTRACT_JS)
            if answers is None:
                qa.harvest_select_options(page, controls)
                answers = qa.get_answers(controls, context={"slug": slug, "url": url})
                answered_keys = {c["id"] or c["name"] for c in controls}
            else:
                # Selecting an option can reveal conditional card fields
                # (Belvedere 2026-08-17: 'Name of School' = Other exposed
                # start-date/essay fields that never got answers). Ask the
                # Q&A engine about controls that were not present on pass 0.
                fresh = [c for c in controls
                         if (c["id"] or c["name"]) not in answered_keys
                         and not c["value"] and not c.get("chosen")]
                if fresh:
                    qa.harvest_select_options(page, fresh)
                    answers += qa.get_answers(fresh, context={"slug": slug, "url": url})
                    answered_keys |= {c["id"] or c["name"] for c in fresh}
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

        required_empty = _lever_required_empty(page)
        result["unanswered"] = required_empty
        _record_filled_screenshot(result, page, slug)

        if required_empty:
            result["ok"] = True
            result["reason"] = _lever_required_reason(required_empty)
            browser.close()
            return result
        if dry_run:
            result["ok"] = True
            result["reason"] = "dry run — did not submit"
            browser.close()
            return result

        if lever_captcha_present(page):
            result.update(
                ok=True,
                submitted=False,
                outcome="manual",
                retryable=False,
                click_attempted=False,
                submission_uncertain=False,
                reason="Lever hCaptcha requires manual completion",
                unanswered=["Lever hCaptcha"],
            )
            _record_filled_screenshot(result, page, slug)
            browser.close()
            return result

        try:
            mark_submit_attempted()
            page.locator("button#btn-submit, button:has-text('Submit application')").first.click(timeout=5000)
            page.wait_for_timeout(5000)
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
    print(apply_lever(sys.argv[1], Path(sys.argv[2]), "lever_test", dry_run=True))
