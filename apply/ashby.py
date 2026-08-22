"""Ashby application adapter. Reuses the qa.py Q&A engine.

jobs.ashbyhq.com/<org>/<uuid> -> application tab has a React form.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa
from ashby_browser import persistent_ashby_context
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"


def _ashby_company_context(url: str) -> str:
    """Return the stable Ashby board token instead of the per-run screenshot slug."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "ashbyhq.com" and not host.endswith(".ashbyhq.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return unquote(parts[0]) if parts else ""


def _shot(page, slug, stage):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(SHOTS / f"{slug}_{stage}.png"),
            full_page=True,
            timeout=1000,
        )
        return True
    except Exception:
        # macOS stops compositing screenshots after System Events hides the
        # dedicated headful process. Diagnostics must never block submission.
        return False


def _ashby_submission_rejection(body_text: str) -> str:
    """Return a safe manual-action reason for Ashby's explicit spam rejection."""
    body = body_text or ""
    if (
        re.search(r"we couldn['’]t submit your application", body, re.IGNORECASE)
        and re.search(r"flagged as possible spam", body, re.IGNORECASE)
    ):
        return (
            "Ashby rejected the submission as possible spam; retry manually from "
            "a trusted browser and network"
        )
    return ""


def _fill_basics(page, profile: dict) -> None:
    """Fill stable Ashby profile fields without treating split names as one field."""
    full_name = f"{profile['name']['first']} {profile['name']['last']}"
    for label, value, exact in [
        ("First Name", profile["name"]["first"], False),
        ("Last Name", profile["name"]["last"], False),
        ("Full Name", full_name, True),
        ("Name", full_name, True),
        ("Email", profile["email"], False),
        ("Phone", profile["phone"], False),
        ("LinkedIn", profile["links"]["linkedin"], False),
        ("GitHub", profile["links"]["github"], False),
    ]:
        try:
            element = page.get_by_label(label, exact=exact).first
            if element.count() and element.is_visible() and not element.input_value():
                element.fill(value)
        except Exception:
            pass


def _run_ashby_form(page, url: str, resume_pdf: Path, slug: str, dry_run: bool) -> dict:
    p = PROFILE
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    # normalize: strip query params (?embed=... breaks the standalone form), ensure /application
    base = url.split("?")[0].rstrip("/")
    apply_url = base if base.endswith("/application") else base + "/application"

    page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)

    # resume upload first: Ashby autofills from it too
    try:
        page.locator("input[type=file]").first.set_input_files(str(resume_pdf))
        page.wait_for_timeout(4500)
    except Exception as e:
        result["reason"] = f"resume upload failed: {e}"
        _shot(page, slug, "fail_upload")
        return result

    # basics via label matching (Ashby ids are generated)
    _fill_basics(page, p)

    answers = None
    filled_qa, failed_qa = [], []
    for qa_pass in range(3):
        controls = page.evaluate(qa.EXTRACT_JS)
        if answers is None:
            qa.harvest_select_options(page, controls)
            answers = qa.get_answers(controls, context={
                "company": _ashby_company_context(apply_url),
                "slug": slug,
                "url": apply_url,
            })
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

    required_empty = page.evaluate(r"""
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
                bad.push(lbl.replace(/\s+/g, ' ').trim().slice(0, 80));
            });
            return [...new Set(bad)];
        }
    """)
    result["unanswered"] = required_empty
    _shot(page, slug, "filled")

    if required_empty:
        result["ok"] = True
        result["reason"] = f"needs answers: {required_empty[:6]}"
        return result
    if dry_run:
        result["ok"] = True
        result["reason"] = "dry run — did not submit"
        return result

    try:
        mark_submit_attempted()
        page.locator("button:has-text('Submit application'), button:has-text('Submit Application')").first.click(timeout=5000)
        page.wait_for_timeout(5000)
        _shot(page, slug, "submitted")
        body = page.inner_text("body").lower()
        rejection = _ashby_submission_rejection(body)
        if rejection:
            result.update(
                ok=False,
                submitted=False,
                outcome="manual",
                retryable=False,
                click_attempted=True,
                submission_uncertain=False,
                definitive_rejection=True,
                reason=rejection,
            )
        elif confirmation_observed(body, page.url):
            result.update(ok=True, submitted=True, reason="confirmed")
        else:
            mark_unconfirmed(result)
    except PWTimeout:
        mark_unconfirmed(result, "submit click timed out; verify possible prior submission")
    return result


def apply_ashby(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    with sync_playwright() as pw:
        with persistent_ashby_context(pw) as ctx:
            page = configure_page(ctx.pages[0] if ctx.pages else ctx.new_page())
            return _run_ashby_form(page, url, resume_pdf, slug, dry_run)


if __name__ == "__main__":
    print(apply_ashby(sys.argv[1], Path(sys.argv[2]), "ashby_test", dry_run=True))
