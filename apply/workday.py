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
from timeouts import configure_page

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
  const ffs = [...document.querySelectorAll('[data-automation-id^="formField-"]')];
  ffs.forEach((ff, idx) => {
    const label = ff.querySelector('label, [data-automation-id="richText"], legend')?.innerText
        ?.replace(/\\s+/g, ' ')?.trim()?.slice(0, 250) || ff.getAttribute('data-automation-id');
    const entry = {faid: (ff.getAttribute('data-fkit-id') || 'x') + '|' + idx,
                   label, kind: 'unknown',
                   value: '', required: !!ff.querySelector('[aria-required="true"], [required]')
                            || /\\*/.test(ff.querySelector('label')?.innerText || '')};
    const dateWrap = ff.querySelector('[data-automation-id="dateInputWrapper"]');
    const txt = ff.querySelector('input[type=text], input[type=email], input[type=tel], input:not([type]), textarea');
    const btn = ff.querySelector('button[aria-haspopup="listbox"]');
    const radios = ff.querySelectorAll('input[type=radio]');
    const checks = ff.querySelectorAll('input[type=checkbox]');
    const multi = ff.querySelector('[data-automation-id="multiSelectContainer"], [data-automation-id*=searchBox]');
    if (dateWrap) {
      entry.kind = 'date';
      const m = dateWrap.querySelector('[data-automation-id="dateSectionMonth-input"]');
      const d = dateWrap.querySelector('[data-automation-id="dateSectionDay-input"]');
      const y = dateWrap.querySelector('[data-automation-id="dateSectionYear-input"]');
      entry.hasDay = !!d;
      entry.value = (m?.value && y?.value) ? `${m.value}/${d?.value ? d.value + '/' : ''}${y.value}` : '';
    } else if (btn) {
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
      entry.kind = checks.length > 1 ? 'checkgroup' : 'checkbox';
      if (checks.length > 1) {
        entry.options = [...checks].map(c => c.labels?.[0]?.innerText?.trim() || c.value).filter(Boolean);
        entry.value = [...checks].find(c => c.checked)?.labels?.[0]?.innerText?.trim() || '';
      } else {
        entry.value = [...checks].some(c => c.checked) ? 'checked' : '';
      }
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


def _claude_pick(question: str, answer_intent: str, options: list[str]) -> str | None:
    """Last-resort: have Claude pick the verbatim option for this question."""
    import urllib.request
    if not options:
        return None
    body = json.dumps({
        "model": qa.MODEL, "max_tokens": 200,
        "messages": [{"role": "user", "content":
            f"Candidate profile intent: {answer_intent}\n"
            f"Application question: {question}\n"
            f"Options: {json.dumps(options)}\n"
            "Candidate facts: Brown University BS; for INTERNSHIP roles graduating May 2028, for full-time May 2027; US citizen; "
            "born 2005; no prior employment at this company.\n"
            "Reply with EXACTLY one option, verbatim, nothing else."}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": qa.API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
        text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text").strip()
        return text if text in options else qa._best_option(text, options)
    except Exception:
        return None


def wd_fill(page, field: dict, answer: str) -> bool:
    """Fill one Workday formField. Address by fkit id when unique (index shifts as
    the DOM mutates), else fall back to extraction index."""
    fkit, _, idx_s = field["faid"].partition("|")
    ff = None
    if fkit and fkit != "x":
        cand = page.locator(f"[data-fkit-id='{fkit}']")
        if cand.count() == 1:
            ff = cand.first
    if ff is None:
        ff = page.locator("[data-automation-id^='formField-']").nth(int(idx_s))
    try:
        ff.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    kind = field["kind"]
    try:
        if kind == "date":
            import datetime as _dt
            ans_s = str(answer)
            m3 = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})", ans_s)
            m2 = re.search(r"(\d{1,2})\s*/\s*(\d{4})", ans_s)
            if field.get("hasDay"):
                if m3:
                    mm, dd, yyyy = m3.group(1), m3.group(2), m3.group(3)
                else:
                    t = _dt.date.today()
                    mm, dd, yyyy = str(t.month), str(t.day), str(t.year)
            else:
                dd = ""
                if m2:
                    mm, yyyy = m2.group(1), m2.group(2)
                else:
                    y = re.search(r"(\d{4})", ans_s)
                    if not y:
                        return False
                    mm, yyyy = "05", y.group(1)
            mi = ff.locator("[data-automation-id='dateSectionMonth-input']").first
            di = ff.locator("[data-automation-id='dateSectionDay-input']").first
            yi = ff.locator("[data-automation-id='dateSectionYear-input']").first
            mi.scroll_into_view_if_needed(timeout=4000)
            page.wait_for_timeout(300)
            mi.evaluate("el => el.focus()")
            page.keyboard.type(mm.zfill(2), delay=120)
            if dd and di.count():
                page.wait_for_timeout(150)
                di.evaluate("el => el.focus()")
                page.keyboard.type(dd.zfill(2), delay=120)
            page.wait_for_timeout(200)
            yi.evaluate("el => el.focus()")
            page.keyboard.type(yyyy, delay=120)
            page.wait_for_timeout(300)
            return bool(mi.evaluate("el => el.value") and yi.evaluate("el => el.value"))
        if kind == "text" or kind == "textarea":
            inp = ff.locator("textarea, input[type=text], input[type=email], input[type=tel], input:not([type])").first
            inp.fill(str(answer))
            return bool(inp.evaluate("el => (el.value||'').trim()"))
        if kind == "dropdown":
            btn = ff.locator("button[aria-haspopup='listbox']").first
            btn.scroll_into_view_if_needed(timeout=4000)
            btn.click(timeout=4000)
            page.wait_for_timeout(900)
            # scope to the OPEN listbox (page may contain other [role=option] noise)
            lb = page.locator("ul[role=listbox]:visible, [role=listbox]:visible").last
            box = lb if lb.count() else page
            opts = [o.strip() for o in box.locator("[role=option]").all_inner_texts()]
            opts = [o for o in opts if o and o.lower() != "select one"]
            target = qa._best_option(str(answer), opts) or (opts[0] if len(opts) == 1 else None)
            if target is None:
                # fuzzy match failed: let Claude pick the verbatim option
                target = _claude_pick(field.get("label", ""), str(answer), opts)
            if target is None:
                page.keyboard.press("Escape")
                return False
            # click by INDEX in the harvested list — never by substring (has-text
            # is case-insensitive so "Male" would match "Female")
            all_opts = [o.strip() for o in box.locator("[role=option]").all_inner_texts()]
            try:
                idx = all_opts.index(target)
            except ValueError:
                idx = next((i for i, o in enumerate(all_opts) if o.strip() == target.strip()), -1)
            if idx < 0:
                page.keyboard.press("Escape")
                return False
            box.locator("[role=option]").nth(idx).click(timeout=4000)
            page.wait_for_timeout(400)
            now = ff.locator("button").first.inner_text().strip()
            return now.strip().lower() == target.strip().lower() or (
                bool(now) and "select one" not in now.lower())
        if kind == "radio":
            radios = ff.locator("input[type=radio]")
            labels = [radios.nth(i).evaluate("el => el.labels?.[0]?.innerText || el.value || ''").strip()
                      for i in range(radios.count())]
            pick = qa._best_option(str(answer), [l for l in labels if l])
            if pick is None:
                return False
            i = labels.index(pick)
            try:
                radios.nth(i).check(timeout=3000)
            except Exception:
                radios.nth(i).evaluate("el => el.labels?.[0]?.click() || el.click()")
            return True
        if kind == "checkgroup":
            checks = ff.locator("input[type=checkbox]")
            labels = [checks.nth(i).evaluate("el => el.labels?.[0]?.innerText || el.value || ''").strip()
                      for i in range(checks.count())]
            pick = qa._best_option(str(answer), [l for l in labels if l])
            if pick is None:
                return False
            i = labels.index(pick)
            try:
                checks.nth(i).check(timeout=3000)
            except Exception:
                checks.nth(i).evaluate("el => el.labels?.[0]?.click() || el.click()")
            return True
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
            page.wait_for_timeout(1200)
            # Try typing first (moniker search)
            try:
                inp.fill(str(answer)[:50])
                page.wait_for_timeout(1600)
            except Exception:
                pass
            opt = page.locator("[data-automation-id='promptOption'], [role=option]")
            opts = [o.strip() for o in opt.all_inner_texts()]
            target = qa._best_option(str(answer), opts)
            if target is None and opts:
                # hierarchical prompt: navigate category -> leaf (up to 2 levels)
                try:
                    inp.fill("")
                except Exception:
                    pass
                page.wait_for_timeout(800)
                for level in range(2):
                    opts = [o.strip() for o in opt.all_inner_texts()]
                    pick = qa._best_option(str(answer), opts) or next(
                        (o for o in opts if o.lower() in ("career websites", "job boards", "other")), None)
                    if not pick:
                        break
                    page.locator(f"[data-automation-id='promptOption']:has-text(\"{pick[:40]}\")").first.click(timeout=4000)
                    page.wait_for_timeout(1200)
                    if ff.locator("[data-automation-id='selectedItem']").count():
                        return True
                opts = [o.strip() for o in opt.all_inner_texts()]
                target = qa._best_option(str(answer), opts) or (opts[0] if opts else None)
            if target:
                page.locator(f"[data-automation-id='promptOption']:has-text(\"{target[:40]}\"), [role=option]:has-text(\"{target[:40]}\")").first.click(timeout=4000)
                page.wait_for_timeout(600)
            page.keyboard.press("Escape")
            return bool(ff.locator("[data-automation-id='selectedItem']").count())
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
        stories=qa._grounding(),
        today=today,
    ).replace('"id_or_name"', '"faid"') + (
        f"\nContext: applying to {company} — {title} via Workday. "
        "Use 'faid' as the key, copied exactly from the input. "
        "For 'How Did You Hear About Us': prefer company website/careers site options. "
        "For source dropdowns with many options, answer with the best guess text; matching is fuzzy. "
        "Date fields (kind='date') expect MM/YYYY. Work experience dates come from the resume in the profile's work_history_summary. "
        "Education From/To: 09/2024 to 05/2028 for internship roles, 09/2024 to 05/2027 for full-time roles (this posting's type decides). Degree dropdown: 'Bachelor of Science (B.S.)' or closest BS option. "
        "If a 'To' date field pairs with an 'I currently work here' checkbox, give the real end date instead of checking it."
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


def _account_scope(page):
    """The account form can render in the main page or an iframe (Medtronic).
    Return the frame that actually contains it, else the page itself.
    Polls briefly: the iframe often loads AFTER domcontentloaded."""
    for _ in range(6):  # up to ~12s
        for f in page.frames:
            try:
                if f.locator("[data-automation-id='createAccountSubmitButton'], "
                             "[data-automation-id='signInSubmitButton']").count():
                    return f
            except Exception:
                continue
        page.wait_for_timeout(2000)
    return page


def maybe_create_account(page, company_key: str) -> None:
    """Some tenants interpose account creation. Use profile email + stored password."""
    scope = _account_scope(page)
    if not scope.locator("[data-automation-id='createAccountSubmitButton']").count():
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
    scope.locator("input[data-automation-id='email']").fill(PROFILE["email"])
    scope.locator("input[data-automation-id='password']").fill(pw)
    vp = scope.locator("input[data-automation-id='verifyPassword']")
    if vp.count():
        vp.fill(pw)
    cb = scope.locator("input[data-automation-id='createAccountCheckbox']")
    if cb.count():
        try:
            cb.check()
        except Exception:
            cb.evaluate("el => el.click()")
    # Workday overlays the real button with a click_filter div that intercepts
    # pointer events (Medtronic trace 2026-08-09): click the overlay if present.
    overlay = scope.locator("[data-automation-id='click_filter'][aria-label='Create Account']")
    if overlay.count():
        overlay.first.click()
    else:
        scope.locator("[data-automation-id='createAccountSubmitButton']").click()
    page.wait_for_timeout(4000)


def maybe_sign_in(page, company_key: str) -> None:
    """If tenant bounces to sign-in (account exists), use stored credentials."""
    scope = _account_scope(page)
    if not scope.locator("[data-automation-id='signInSubmitButton']").count():
        return
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT email, password FROM wd_accounts WHERE tenant=?", (company_key,)).fetchone()
    conn.close()
    if not row:
        return
    scope.locator("input[data-automation-id='email']").fill(row[0])
    scope.locator("input[data-automation-id='password']").fill(row[1])
    overlay = scope.locator("[data-automation-id='click_filter'][aria-label='Sign In']")
    if overlay.count():
        overlay.first.click()
    else:
        scope.locator("[data-automation-id='signInSubmitButton']").click()
    page.wait_for_timeout(4000)


def fill_current_page(page, company_key: str, slug: str) -> None:
    """One extraction + Claude answering + fill pass over the current wizard page."""
    fields = page.evaluate(WD_EXTRACT_JS)
    force_identity(page, fields)
    fields = page.evaluate(WD_EXTRACT_JS)
    for f in fields:
        if f["kind"] == "dropdown" and not f["value"]:
            try:
                fkit, _, idx_s = f["faid"].partition("|")
                ff = page.locator(f"[data-fkit-id='{fkit}']").first if fkit != "x" else \
                    page.locator("[data-automation-id^='formField-']").nth(int(idx_s))
                btn = ff.locator("button[aria-haspopup='listbox']").first
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=3000)
                page.wait_for_timeout(700)
                lb = page.locator("ul[role=listbox]:visible, [role=listbox]:visible").last
                f["options"] = [o.strip() for o in lb.locator("[role=option]").all_inner_texts()
                                if o.strip() and o.strip().lower() != "select one"][:40]
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                page.keyboard.press("Escape")
    todo = [f for f in fields if not f["value"]]
    if not todo:
        return
    answers = wd_answers(fields, company_key, slug)
    amap = {a["faid"]: a["answer"] for a in answers if "faid" in a}
    for f in todo:
        if f["faid"] in amap:
            wd_fill(page, f, str(amap[f["faid"]]))
            page.wait_for_timeout(250)


def apply_workday(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    company_key = url.split("//")[1].split(".")[0]  # tenant subdomain
    result = {"ok": False, "submitted": False, "reason": "", "pages": [], "unanswered": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 1400})
        page = configure_page(ctx.new_page())
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        # Apply -> autofill with resume
        btn = page.locator("a[data-automation-id='adventureButton'], button[data-automation-id='adventureButton']").first
        if not btn.count():
            result["reason"] = "apply button not found (posting closed?)"
            browser.close()
            return result
        # cookie banner steals the first click on some tenants
        cb = page.locator("[data-automation-id='legalNoticeAcceptButton'], #onetrust-accept-btn-handler").first
        try:
            if cb.count() and cb.is_visible():
                cb.click(timeout=3000)
                page.wait_for_timeout(800)
        except Exception:
            pass
        af = page.locator("[data-automation-id='autofillWithResume']").first
        for attempt in range(3):
            try:
                btn.click(timeout=5000)
            except Exception:
                pass
            try:
                af.wait_for(state="visible", timeout=6000)
                break
            except Exception:
                page.wait_for_timeout(1000)
        # Some tenants (Medtronic) don't navigate on the Apply click: go directly
        # to the canonical autofill route, which surfaces the account gate.
        # NOTE: the account form may live in an IFRAME (Medtronic), so check frames.
        if not (af.count() and af.is_visible()) and \
                _account_scope(page) is page:
            page.goto(url.rstrip("/") + "/apply/autofillWithResume",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
        maybe_create_account(page, company_key)
        maybe_sign_in(page, company_key)
        # after account creation/sign-in the autofill choice may render fresh
        try:
            af.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        if af.count() and af.is_visible():
            af.click(timeout=8000)
            page.wait_for_timeout(3000)
            # Medtronic ordering: the Create Account gate appears AFTER choosing
            # autofill (debug trace 2026-08-09). Re-check it before expecting
            # the upload zone.
            maybe_create_account(page, company_key)
            maybe_sign_in(page, company_key)
        up = page.locator("[data-automation-id='file-upload-input-ref']").first
        try:
            up.wait_for(state="attached", timeout=20000)
            up.set_input_files(str(resume_pdf))
            page.wait_for_timeout(5000)
        except Exception:
            result["reason"] = "resume upload zone never appeared"
            _shot(page, slug, "fail_upload")
            browser.close()
            return result

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

            # Fill-and-advance with up to 3 passes per step. Conditional questions
            # appear after earlier answers, so each pass re-extracts + re-harvests.
            advanced = False
            for fill_pass in range(3):
                fill_current_page(page, company_key, slug)
                if fill_pass == 0:
                    _shot(page, slug, f"page{page_no}_{step[:20].replace(' ', '_')}")
                    result["pages"].append(step or f"page{page_no}")
                nxt = page.locator("[data-automation-id='pageFooterNextButton']").first
                if not nxt.count():
                    result["reason"] = f"no Next button on step '{step}'"
                    browser.close()
                    return result
                nxt.click(timeout=8000)
                page.wait_for_timeout(4500)
                if not wd_page_errors(page):
                    advanced = True
                    break
            if not advanced:
                # Stale error banners / aria-invalid can linger after fields are
                # actually filled. If nothing required is empty, push Next again.
                really_empty = [f["label"] for f in page.evaluate(WD_EXTRACT_JS)
                                if f["required"] and not f["value"]]
                if not really_empty:
                    for _ in range(2):
                        nxt = page.locator("[data-automation-id='pageFooterNextButton']").first
                        if not nxt.count():
                            break
                        nxt.click(timeout=8000)
                        page.wait_for_timeout(4500)
                        new_step = current_step(page)
                        if new_step != step:
                            advanced = True
                            break
            if not advanced:
                errs = wd_page_errors(page)
                result["reason"] = f"stuck on '{step}': {errs[:3]}"
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
