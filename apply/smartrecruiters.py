"""SmartRecruiters application adapter. Reuses the qa.py Q&A engine.

jobs.smartrecruiters.com/<Company>/<postingId>[-slug] -> the posting page has an
"I'm interested"/Apply button revealing a one-page React form (name/email/phone,
resume upload, screening questions). Simpler than Greenhouse.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa
import stealth
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _preflight_outcome(body_text: str, captcha_present: bool = False) -> dict | None:
    body = body_text.lower()
    if "job has expired" in body or "job is no longer available" in body:
        return {"outcome": "stale", "reason": "posting expired"}
    datadome_markers = ("datadome", "captcha-delivery.com", "verify you are human")
    if captcha_present or any(marker in body for marker in datadome_markers):
        return {
            "ok": True,
            "outcome": "manual",
            "retryable": False,
            "click_attempted": False,
            "submission_uncertain": False,
            "reason": "SmartRecruiters CAPTCHA requires manual completion",
            "unanswered": ["SmartRecruiters CAPTCHA"],
        }
    return None


def _hiring_team_message(profile: dict) -> str:
    """Build a short factual intro without duplicating the resume graduation date."""
    education = profile["education"]
    return (
        f"{education['school']} {education['major']} student "
        f"(GPA {education['gpa']}) with production backend/ML internship experience "
        "(YC startups Framewise, Freya) and USACO background. Resume attached."
    )


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


def apply_smartrecruiters(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    with sync_playwright() as pw:
        browser, ctx = stealth.launch_stealth_context(pw)
        page = configure_page(ctx.new_page())
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        blocked = _preflight_outcome(page.inner_text("body"))
        if blocked:
            result.update(blocked)
            browser.close()
            return result

        # cookie banner
        for sel in ["#st-accept", "button:has-text('Accept')", "#onetrust-accept-btn-handler"]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.click(timeout=2000)
                    break
            except Exception:
                pass

        # reveal the application form: follow the oneclick-ui link like a human click
        try:
            link = page.locator("a:has-text(\"I'm interested\")").first
            if link.count():
                link.scroll_into_view_if_needed()
                page.wait_for_timeout(800)
                link.click(timeout=5000)
                page.wait_for_timeout(4000)
        except Exception:
            pass

        # SmartRecruiters protects some active oneclick forms with DataDome on
        # cloud IPs. That is a human CAPTCHA gate, not a missing resume widget.
        captcha = page.locator(
            "iframe[title*='DataDome' i], iframe[src*='captcha-delivery.com']"
        )
        blocked = _preflight_outcome(page.inner_text("body"), captcha.count() > 0)
        if blocked:
            result.update(blocked)
            _shot(page, slug, "captcha")
            browser.close()
            return result

        # resume upload: the FIRST file input is often the avatar; find the one
        # scoped to the Resume/Easy Apply sections (accepts documents)
        try:
            up = None
            for i in range(page.locator("input[type=file]").count()):
                cand = page.locator("input[type=file]").nth(i)
                accept = (cand.get_attribute("accept") or "").lower()
                if "pdf" in accept or "doc" in accept:
                    up = cand
                    break
                near = cand.evaluate(
                    "el => el.closest('section, [class*=resume i], [class*=apply i]')?.innerText?.slice(0,200) || ''")
                if re.search(r"resume|cv|easy apply", near, re.I):
                    up = cand
                    break
            if up is None:
                raise RuntimeError("no resume-appropriate file input")
            up.set_input_files(str(resume_pdf))
            page.wait_for_timeout(6000)
        except Exception as e:
            result["reason"] = f"resume upload not found: {e}"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

        # basics by label/name
        # force-overwrite identity fields — SR autofills Email from the resume
        # (school email) while Confirm gets profile email -> mismatch error
        for label, val in [
            ("First name", p["name"]["first"]),
            ("Last name", p["name"]["last"]),
            ("City", p["location"]["city"]),
            ("Phone number", re.sub(r"[^0-9]", "", p["phone"])),
            ("LinkedIn", p["links"]["linkedin"]),
            ("Website", p["links"]["github"]),
        ]:
            try:
                el = page.get_by_label(label, exact=False).first
                if el.count() and el.is_visible() and not el.input_value():
                    el.fill(val)
                    page.wait_for_timeout(200)
            except Exception:
                pass
        for label in ("Email", "Confirm your email"):
            try:
                el = page.get_by_label(label, exact=False).first
                if el.count() and el.is_visible():
                    el.fill("")
                    el.fill(p["email"])
                    page.wait_for_timeout(200)
            except Exception:
                pass
        # short message to hiring team from the tailored angle
        try:
            msg_box = page.locator("textarea").first
            if msg_box.count() and msg_box.is_visible() and not msg_box.input_value():
                msg_box.fill(_hiring_team_message(p))
        except Exception:
            pass
        # multi-step: click Next through screening pages, running QA each page
        for _ in range(4):
            nxt = page.locator("button:has-text('Next')").first
            if not nxt.count() or not nxt.is_visible():
                break
            nxt.click(timeout=4000)
            page.wait_for_timeout(3000)

        # Q&A engine for screening questions (multi-pass)
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
                document.querySelectorAll('[aria-required="true"], [required]').forEach(el => {
                    if (el.getAttribute('aria-hidden') === 'true' || el.type === 'file') return;
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        if ([...document.querySelectorAll('input')].filter(x => x.name === el.name).some(x => x.checked)) return;
                    } else if (String(el.value || '').trim()) return;
                    const lbl = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || el.name || 'unknown';
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
            page.locator("button:has-text('Submit'), button[type=submit]").first.click(timeout=6000)
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
    print(apply_smartrecruiters(sys.argv[1], Path(sys.argv[2]), "sr_test", dry_run=True))
