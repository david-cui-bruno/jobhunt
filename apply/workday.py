"""Workday application adapter.

Workday tenants share a common UI built on data-automation-id attributes:
  - Apply -> "Autofill with Resume" -> upload -> multi-page wizard
  - Pages: My Information -> My Experience -> Application Questions ->
           Voluntary Disclosures -> Self Identify -> Review -> Submit
  - Some tenants demand account creation; handled via profile email + generated
    password (stored per-company in the tracker) and Gmail verification codes.

Strategy per page: extract Workday formFields (label + control type + options),
ask the shared Claude Q&A brain (qa.answers_for_workday), fill with
widget-specific strategies, verify no required-field errors, then Next.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"
DB = ROOT / "out" / "tracker.db"

MAX_PAGES = 12  # wizard safety bound


def _shot(page, slug, stage):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{slug}_{stage}.png"), full_page=True)


# ---------- Workday-specific control extraction ----------

WD_EXTRACT_JS = """
() => {
  const out = [];
  document.querySelectorAll('[data-automation-id^="formField-"]').forEach(ff => {
    const label = ff.querySelector('label, [data-automation-id="richText"], legend')?.innerText
        ?.replace(/\\s+/g, ' ')?.trim()?.slice(0, 250) || ff.getAttribute('data-automation-id');
    const entry = {faid: ff.getAttribute('data-automation-id'), label, kind: 'unknown',
                   value: '', required: !!ff.querySelector('[aria-required="true"], [required]')
                            || /\\*/.test(ff.querySelector('label')?.innerText || '')};
    const txt = ff.querySelector('input[type=text], input[type=email], input[type=tel], input:not([type]), textarea');
    const btn = ff.querySelector('button[aria-haspopup="listbox"]');
    const radios = ff.querySelectorAll('input[type=radio]');
    const checks = ff.querySelectorAll('input[type=checkbox]');
    const multi = ff.querySelector('[data-automation-id="multiSelectContainer"], [data-automation-id*=searchBox]');
    if (btn) {
      entry.kind = 'dropdown';
      entry.value = btn.innerText.replace(/\\s+/g,' ').trim();
      if (/select one|^$/i.test(entry.value)) entry.value = '';
    } else if (multi) {
      entry.kind = 'multiselect';
      entry.value = [...ff.querySelectorAll('[data-automation-id="selectedItem"]')].map(x => x.innerText.trim()).join('; ');
    } else if (radios.length) {
      entry.kind = 'radio';
      entry.options = [...radios].map(r => r.labels?.[0]?.innerText?.trim() || r.value);
      entry.value = [...radios].find(r => r.checked)?.labels?.[0]?.innerText?.trim() || '';
    } else if (checks.length) {
      entry.kind = 'checkbox';
      entry.value = [...checks].some(c => c.checked) ? 'checked' : '';
    } else if (txt) {
      entry.kind = txt.tagName === 'TEXTAREA' ? 'textarea' : 'text';
      entry.value = txt.value || '';
    } else {
      return; // display-only field
    }
    out.push(entry);
  });
  return out;
}
"""


def wd_fill(page, field: dict, answer: str) -> bool:
    """Fill one Workday formField by automation id. Returns success."""
    faid = field["faid"]
    ff = page.locator(f"[data-automation-id='{faid}']").first
    try:
        ff.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    kind = field["kind"]
    try:
        if kind == "text" or kind == "textarea":
            inp = ff.locator("input, textarea").first
            inp.fill(str(answer))
            return bool(inp.evaluate("el => (el.value||'').trim()"))
        if kind == "dropdown":
            btn = ff.locator("button[aria-haspopup='listbox']").first
            btn.click(timeout=4000)
            page.wait_for_timeout(700)
            opts = [o.strip() for o in page.locator("[role=option]").all_inner_texts()]
            target = qa._best_option(str(answer), opts) or (opts[0] if len(opts) == 1 else None)
            if target is None:
                page.keyboard.press("Escape")
                return False
            page.locator(f"[role=option]:has-text(\"{target[:45]}\")").first.click(timeout=4000)
            page.wait_for_timeout(400)
            return target.lower() in ff.locator("button").first.inner_text().lower()
        if kind == "radio":
            radios = ff.locator("input[type=radio]")
            for i in range(radios.count()):
                lab = radios.nth(i).evaluate("el => el.labels?.[0]?.innerText || el.value || ''").strip()
                if qa._best_option(str(answer), [lab]):
                    try:
                        radios.nth(i).check(timeout=3000)
                    except Exception:
                        radios.nth(i).evaluate("el => el.labels?.[0]?.click() || el.click()")
                    return True
            return False
        if kind == "checkbox":
            box = ff.locator("input[type=checkbox]").first
            if str(answer).lower() in ("yes", "true", "1", "on", "checked", "agree"):
                try:
                    box.check(timeout=3000)
                except Exception:
                    box.evaluate("el => el.click()")
            return True
        if kind == "multiselect":
            inp = ff.locator("input").first
            inp.click(timeout=3000)
            inp.fill(str(answer)[:50])
            page.wait_for_timeout(1500)
            opt = page.locator("[data-automation-id='promptOption'], [role=option]").first
            if opt.count():
                opt.click(timeout=4000)
                page.wait_for_timeout(400)
                return True
            page.keyboard.press("Escape")
            return False
    except Exception:
        return False
    return False


def wd_answers(fields: list[dict], company: str, title: str) -> list[dict]:
    """Claude maps Workday fields -> answers, same policy brain as qa.py."""
    import urllib.request
    import datetime
    unanswered = [f for f in fields if not f["value"]]
    if not unanswered:
        return []
    today = datetime.date.today().strftime("%m/%d/%Y")
    prompt = qa.ANSWER_PROMPT.format(
        profile=yaml.dump(PROFILE),
        controls=json.dumps(unanswered)[:20000],
        today=today,
    ).replace('"id_or_name"', '"faid"') + (
        f"\nContext: applying to {company} — {title} via Workday. "
        "Use 'faid' as the key, copied exactly from the input. "
        "For 'How Did You Hear About Us': prefer company website/careers site options. "
        "For source dropdowns with many options, answer with the best guess text; matching is fuzzy."
    )
    body = json.dumps({
        "model": qa.MODEL, "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": qa.API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


def wd_page_errors(page) -> list[str]:
    errs = page.locator(
        "[data-automation-id='errorMessage'], [data-automation-id='errorBanner'], "
        "[data-automation-id*=alert], [aria-invalid='true']").all_inner_texts()
    # aria-invalid inputs have no text; count them too
    n_invalid = page.locator("[aria-invalid='true']").count()
    out = [e.strip() for e in errs if e.strip()][:8]
    if n_invalid and not out:
        out = [f"{n_invalid} invalid fields"]
    return out


# Identity fields the resume-parser routinely mangles: always overwrite from profile.
FORCE_FIELDS = [
    (re.compile(r"^first name", re.I), lambda p: p["name"]["first"]),
    (re.compile(r"^last name", re.I), lambda p: p["name"]["last"]),
    (re.compile(r"^email", re.I), lambda p: p["email"]),
    (re.compile(r"^address line 1", re.I), lambda p: p["location"].get("street", "")),
    (re.compile(r"^city", re.I), lambda p: p["location"]["city"]),
    (re.compile(r"^state", re.I), lambda p: {"RI": "Rhode Island"}.get(p["location"]["state"], p["location"]["state"])),
    (re.compile(r"^postal code|^zip", re.I), lambda p: p["location"].get("zip", "")),
    (re.compile(r"^phone number", re.I), lambda p: re.sub(r"[^0-9]", "", p["phone"])),
    (re.compile(r"^phone device type", re.I), lambda p: "Mobile"),
]


def force_identity(page, fields: list[dict]) -> None:
    for f in fields:
        for rx, getter in FORCE_FIELDS:
            if rx.match(f["label"] or ""):
                val = getter(PROFILE)
                if val and str(val) != f["value"]:
                    wd_fill(page, f, str(val))
                    page.wait_for_timeout(200)
                break


def current_step(page) -> str:
    try:
        el = page.locator("[data-automation-id='progressBarActiveStep']").first
        if el.count():
            return el.inner_text().strip()
    except Exception:
        pass
    return ""


def maybe_create_account(page, company_key: str) -> None:
    """Some tenants interpose account creation. Use profile email + stored password."""
    if not page.locator("[data-automation-id='createAccountSubmitButton']").count():
        return
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS wd_accounts (tenant TEXT PRIMARY KEY, email TEXT, password TEXT, created_at INTEGER)")
    row = conn.execute("SELECT password FROM wd_accounts WHERE tenant=?", (company_key,)).fetchone()
    pw = row[0] if row else ("Jh!" + secrets.token_urlsafe(12) + "9x")
    if not row:
        conn.execute("INSERT INTO wd_accounts VALUES (?,?,?,?)",
                     (company_key, PROFILE["email"], pw, int(time.time())))
        conn.commit()
    conn.close()
    page.locator("input[data-automation-id='email']").fill(PROFILE["email"])
    page.locator("input[data-automation-id='password']").fill(pw)
    vp = page.locator("input[data-automation-id='verifyPassword']")
    if vp.count():
        vp.fill(pw)
    cb = page.locator("input[data-automation-id='createAccountCheckbox']")
    if cb.count():
        try:
            cb.check()
        except Exception:
            cb.evaluate("el => el.click()")
    page.locator("[data-automation-id='createAccountSubmitButton']").click()
    page.wait_for_timeout(4000)


def maybe_sign_in(page, company_key: str) -> None:
    """If tenant bounces to sign-in (account exists), use stored credentials."""
    if not page.locator("[data-automation-id='signInSubmitButton']").count():
        return
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT email, password FROM wd_accounts WHERE tenant=?", (company_key,)).fetchone()
    conn.close()
    if not row:
        return
    page.locator("input[data-automation-id='email']").fill(row[0])
    page.locator("input[data-automation-id='password']").fill(row[1])
    page.locator("[data-automation-id='signInSubmitButton']").click()
    page.wait_for_timeout(4000)


def apply_workday(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    company_key = url.split("//")[1].split(".")[0]  # tenant subdomain
    result = {"ok": False, "submitted": False, "reason": "", "pages": [], "unanswered": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 1400})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        # Apply -> autofill with resume
        btn = page.locator("a[data-automation-id='adventureButton'], button[data-automation-id='adventureButton']").first
        if not btn.count():
            result["reason"] = "apply button not found (posting closed?)"
            browser.close()
            return result
        btn.click()
        page.wait_for_timeout(2500)
        maybe_create_account(page, company_key)
        maybe_sign_in(page, company_key)
        af = page.locator("[data-automation-id='autofillWithResume']").first
        if af.count():
            af.click(timeout=8000)
            page.wait_for_timeout(2500)
        up = page.locator("[data-automation-id='file-upload-input-ref']").first
        if up.count():
            up.set_input_files(str(resume_pdf))
            page.wait_for_timeout(5000)

        # wizard loop
        for page_no in range(MAX_PAGES):
            step = current_step(page)
            body_text = page.inner_text("body").lower()
            if "review" in step.lower():
                _shot(page, slug, f"review")
                if dry_run:
                    result.update(ok=True, reason="dry run — reached Review, did not submit")
                    browser.close()
                    return result
                sub = page.locator("button:has-text('Submit'), [data-automation-id='pageFooterNextButton']").first
                sub.click(timeout=8000)
                page.wait_for_timeout(6000)
                _shot(page, slug, "submitted")
                body = page.inner_text("body").lower()
                confirmed = any(w in body for w in ("thank", "congratulations", "submitted", "received"))
                result.update(ok=True, submitted=True,
                              reason="confirmed" if confirmed else "submitted (no confirm text)")
                browser.close()
                return result

            fields = page.evaluate(WD_EXTRACT_JS)
            force_identity(page, fields)
            fields = page.evaluate(WD_EXTRACT_JS)
            todo = [f for f in fields if not f["value"]]
            if todo:
                answers = wd_answers(fields, company_key, slug)
                amap = {a["faid"]: a["answer"] for a in answers if "faid" in a}
                for f in todo:
                    if f["faid"] in amap:
                        wd_fill(page, f, str(amap[f["faid"]]))
                        page.wait_for_timeout(250)
            _shot(page, slug, f"page{page_no}_{step[:20].replace(' ', '_')}")
            result["pages"].append(step or f"page{page_no}")

            nxt = page.locator("[data-automation-id='pageFooterNextButton']").first
            if not nxt.count():
                result["reason"] = f"no Next button on step '{step}'"
                browser.close()
                return result
            nxt.click(timeout=8000)
            page.wait_for_timeout(4500)
            errs = wd_page_errors(page)
            if errs:
                # one retry: re-extract and refill missing required fields
                fields = page.evaluate(WD_EXTRACT_JS)
                missing = [f for f in fields if f["required"] and not f["value"]]
                if missing:
                    answers = wd_answers(missing, company_key, slug)
                    amap = {a["faid"]: a["answer"] for a in answers if "faid" in a}
                    for f in missing:
                        if f["faid"] in amap:
                            wd_fill(page, f, str(amap[f["faid"]]))
                    nxt.click(timeout=8000)
                    page.wait_for_timeout(4500)
                errs2 = wd_page_errors(page)
                if errs2:
                    result["reason"] = f"stuck on '{step}': {errs2[:3]}"
                    result["unanswered"] = [f["label"] for f in page.evaluate(WD_EXTRACT_JS)
                                            if f["required"] and not f["value"]][:10]
                    _shot(page, slug, "stuck")
                    browser.close()
                    return result
        result["reason"] = "wizard exceeded max pages"
        browser.close()
    return result


if __name__ == "__main__":
    print(apply_workday(sys.argv[1], Path(sys.argv[2]), "wd_test", dry_run=True))
