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

import base64
import html
import json
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import qa
from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
from timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"
DB = ROOT / "out" / "tracker.db"

MAX_PAGES = 12  # wizard safety bound
CREATE_ACCOUNT_OVERLAY = (
    "[data-automation-id='click_filter'][aria-label*='Create Account'], "
    "[data-automation-id='click_filter'][aria-label*='CreateAccount']"
)
SIGN_IN_OVERLAY = (
    "[data-automation-id='click_filter'][aria-label*='Sign In'], "
    "[data-automation-id='click_filter'][aria-label*='SignIn']"
)


class UnsafePrefilledAnswers(RuntimeError):
    def __init__(self, labels: list[str]):
        self.labels = labels
        super().__init__(f"unsafe prefilled answers: {labels}")


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
                            || /\\*/.test(label)};
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
            "Candidate facts: Brown University BS, expected graduation June 2028 for every role; US citizen. "
            "Do not infer birth date, prior employment, referrals, or other facts not present in the answer intent.\n"
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


def _workday_date_parts(answer: str, has_day: bool) -> tuple[str, str, str] | None:
    """Parse only dates actually supplied by the grounded answer.

    Workday date widgets must fail closed. Substituting today's date for an
    unknown birth/start/end date silently turns missing profile data into a
    false application answer.
    """
    value = str(answer)
    full = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})", value)
    month_year = re.search(r"(\d{1,2})\s*/\s*(\d{4})", value)
    if has_day:
        if not full:
            return None
        return full.group(1).zfill(2), full.group(2).zfill(2), full.group(3)
    # A grounded full date may be rendered into a Workday widget that only asks
    # for month/year.  Parse it before the shorter pattern, otherwise the
    # DD/YYYY tail of 09/08/2026 is misread as August 2026.
    if full:
        month = int(full.group(1))
        return (str(month).zfill(2), "", full.group(3)) if 1 <= month <= 12 else None
    if month_year:
        month = int(month_year.group(1))
        return (str(month).zfill(2), "", month_year.group(2)) if 1 <= month <= 12 else None
    month_names = {
        name.lower(): str(index).zfill(2)
        for index, name in enumerate(
            ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"),
            start=1,
        )
    }
    named = re.search(
        r"\b(" + "|".join(month_names) + r")\s+(\d{4})\b",
        value,
        re.I,
    )
    if named:
        return month_names[named.group(1).lower()], "", named.group(2)
    # A bare year cannot truthfully populate a month/year widget.
    return None


def _checkgroup_targets(answer: object, labels: list[str]) -> list[str]:
    """Resolve one or more requested checkbox labels without arbitrary choices."""
    requested = answer if isinstance(answer, list) else [answer]
    targets = []
    for value in requested:
        pick = qa._best_option(str(value), [label for label in labels if label])
        if pick is None:
            return []
        if pick not in targets:
            targets.append(pick)
    return targets


def wd_fill(page, field: dict, answer: object) -> bool:
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
            parts = _workday_date_parts(str(answer), bool(field.get("hasDay")))
            if parts is None:
                return False
            mm, dd, yyyy = parts
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
            targets = _checkgroup_targets(answer, labels)
            if not targets:
                return False
            for target in targets:
                i = labels.index(target)
                try:
                    checks.nth(i).check(timeout=3000)
                except Exception:
                    checks.nth(i).evaluate("el => el.labels?.[0]?.click() || el.click()")
            return all(checks.nth(labels.index(target)).is_checked() for target in targets)
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
                # Never choose an arbitrary first option. An unmatched answer
                # must remain empty so the required-field gate routes it to
                # manual input instead of submitting a false value.
                target = qa._best_option(str(answer), opts)
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
    policy_fields = [dict(field, company_context=company) for field in unanswered]
    explicit = qa.explicit_approved_answers(
        unanswered,
        key_field="faid",
        company_context=company,
    )
    explicit_keys = {answer["faid"] for answer in explicit}
    model_fields = [field for field in unanswered if field.get("faid") not in explicit_keys]
    model_answers = []
    if model_fields:
        model_answers = _workday_model_answers(model_fields, company, title)
    answers, blocked = qa.filter_manual_answers(
        policy_fields, explicit + model_answers, key_field="faid"
    )
    qa.log_answer_decisions(
        policy_fields,
        answers,
        blocked,
        context={"company": company, "title": title, "ats": "workday"},
        key_field="faid",
    )
    return answers


def _workday_model_answers(fields: list[dict], company: str, title: str) -> list[dict]:
    """Ask the model only for facts not directly rendered from approved answers."""
    import datetime
    import urllib.request
    today = datetime.date.today().strftime("%m/%d/%Y")
    prompt = qa.ANSWER_PROMPT.format(
        profile=yaml.dump(PROFILE),
        application_answers=yaml.safe_dump(
            qa.relevant_application_answers(fields, company_context=company)
        ),
        controls=json.dumps(fields)[:20000],
        stories=qa._grounding(),
        today=today,
    ).replace('"id_or_name"', '"faid"') + (
        f"\nContext: applying to {company} — {title} via Workday. "
        "Use 'faid' as the key, copied exactly from the input. "
        "For 'How Did You Hear About Us': prefer company website/careers site options. "
        "For source dropdowns with many options, answer with the best guess text; matching is fuzzy. "
        "Date fields (kind='date') expect MM/YYYY. Work experience dates come from the resume in the profile's work_history_summary. "
        "Education From/To: 09/2024 to 06/2028 for every role. For a required exact graduation day, use the approved estimate 06/01/2028. Degree dropdown: 'Bachelor of Science (B.S.)' or closest BS option. "
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
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except Exception as exc:
        print(f"[workday] answer model unavailable: {type(exc).__name__}", file=sys.stderr)
        return []
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


def unsafe_prefilled_fields(fields: list[dict], company: str,
                            approved_answers: dict | None = None) -> list[str]:
    """Find sensitive answers already present in a saved draft or resume parse.

    Workday persists drafts and can prefill answers before this worker runs. Those
    values need the same approval and consistency checks as model-generated ones.
    """
    unsafe = []
    for field in fields:
        value = field.get("value")
        if not value:
            continue
        policy_field = dict(field, company_context=company)
        if qa.answer_requires_manual(
            policy_field,
            value,
            approved_answers=approved_answers,
        ):
            unsafe.append(field.get("label") or field.get("faid") or "unknown field")
    return unsafe


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


def saved_draft_wizard_is_active(page) -> bool:
    """Whether Workday resumed directly into a previously saved application."""
    return bool(
        current_step(page)
        and page.locator("[data-automation-id='pageFooterNextButton']").count()
        and page.locator("[data-automation-id^='formField-']").count()
    )


def refresh_saved_resume(page, resume_pdf: Path) -> bool:
    """Replace the same-named resume in a resumed Workday draft.

    A saved draft bypasses the initial autofill upload screen. The attachment may
    therefore predate a resume correction even though its filename is unchanged.
    Delete only the exact same-named file, then upload the current verified PDF.
    If Workday's controls do not match that safe shape, fail closed.
    """
    upload = page.locator("input[data-automation-id='file-upload-input-ref']").first
    if not upload.count():
        return False
    expected_label = f"Delete {resume_pdf.name}"
    existing = page.locator(
        f"button[data-automation-id='delete-file'][aria-label={json.dumps(expected_label)}]"
    )
    if existing.count() != 1:
        return False
    try:
        existing.first.click(timeout=5000)
        page.wait_for_timeout(700)
        dialog = page.locator("[role='dialog']:visible").last
        if dialog.count():
            confirm = dialog.locator(
                "button[data-automation-id*='delete'], button:has-text('Delete')"
            ).last
            if confirm.count() and confirm.is_visible():
                confirm.click(timeout=5000)
                page.wait_for_timeout(700)
        try:
            existing.first.wait_for(state="detached", timeout=5000)
        except Exception:
            return False
        upload = page.locator("input[data-automation-id='file-upload-input-ref']").first
        upload.set_input_files(str(resume_pdf))
        page.wait_for_timeout(4000)
        replacement = page.locator(
            f"button[data-automation-id='delete-file'][aria-label={json.dumps(expected_label)}]"
        )
        return replacement.count() == 1
    except Exception:
        return False


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


def _account_record(company_key: str):
    """Return the saved Workday account without creating tracker state."""
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(
            "SELECT email, password, created_at FROM wd_accounts WHERE tenant=?",
            (company_key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _visible_locator_in_frames(page, selector: str):
    """Return the first visible match across Workday's main page and auth frames."""
    for frame in reversed(page.frames):
        try:
            locator = frame.locator(selector).last
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _open_email_auth(page, create_account: bool) -> None:
    """Open Workday's email auth form from the newer social-login chooser."""
    form_selector = (
        "[data-automation-id='createAccountSubmitButton'], "
        "[data-automation-id='signInSubmitButton']"
    )
    if _visible_locator_in_frames(page, form_selector) is not None:
        return

    email_choice = _visible_locator_in_frames(
        page, "[data-automation-id='SignInWithEmailButton']"
    )
    if email_choice is None:
        utility = _visible_locator_in_frames(
            page, "[data-automation-id='utilityButtonSignIn']"
        )
        if utility is not None:
            utility.click(timeout=5000)
            page.wait_for_timeout(700)
        email_choice = _visible_locator_in_frames(
            page, "[data-automation-id='SignInWithEmailButton']"
        )
    if email_choice is not None:
        try:
            email_choice.click(timeout=5000)
        except Exception:
            email_choice.click(timeout=5000, force=True)
        page.wait_for_timeout(700)
    if create_account:
        create = _visible_locator_in_frames(
            page, "[data-automation-id='createAccountLink']"
        )
        if create is not None:
            create.click(timeout=5000, force=True)
            page.wait_for_timeout(700)


def _message_bodies(part: dict):
    data = part.get("body", {}).get("data")
    if data:
        try:
            padded = data + "=" * (-len(data) % 4)
            yield base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
        except Exception:
            pass
    for child in part.get("parts", []) or []:
        yield from _message_bodies(child)


def workday_activation_url_from_message(message: dict, expected_host: str) -> str | None:
    """Extract only a same-tenant Workday activation URL from a Gmail message."""
    for body in _message_bodies(message.get("payload", {})):
        for raw in re.findall(r"https?://[^\s<>\"']+", html.unescape(body)):
            url = html.unescape(raw).rstrip(".,);]")
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc.lower() != expected_host.lower():
                continue
            if re.search(r"/activate/[^/?#]+", parsed.path):
                return url
    return None


def fetch_workday_activation_url(company_key: str, expected_host: str,
                                  not_before: int = 0) -> str | None:
    """Poll Gmail for the newest activation link for exactly one Workday tenant."""
    notify = ROOT / "notify"
    if str(notify) not in sys.path:
        sys.path.insert(0, str(notify))
    import mailer

    after = max(not_before - 300, int(time.time()) - 2 * 86400)
    query = urllib.parse.quote(
        f'after:{after} from:{company_key}@otp.workday.com '
        'subject:"Verify your candidate account"'
    )
    for attempt in range(6):
        data = mailer._call(f"/messages?q={query}&maxResults=10")
        for item in data.get("messages", []) or []:
            message = mailer._call(f"/messages/{item['id']}?format=full")
            internal = int(message.get("internalDate", 0)) // 1000
            if internal and internal < after:
                continue
            url = workday_activation_url_from_message(message, expected_host)
            if url:
                return url
        if attempt < 5:
            time.sleep(6)
    return None


def _verification_required(page) -> bool:
    phrases = ("verify your account", "verify your candidate account",
               "email has been sent to you")
    for frame in page.frames:
        try:
            body = frame.locator("body").inner_text().lower()
            if any(phrase in body for phrase in phrases):
                return True
        except Exception:
            continue
    return False


def _workday_auth_error(page) -> str:
    """Return the visible Workday authentication error, if any."""
    for frame in reversed(page.frames):
        try:
            error = frame.locator("[data-automation-id='errorMessage']").last
            if error.count() and error.is_visible():
                return error.inner_text().strip()
        except Exception:
            continue
    return ""


def _workday_auth_gate_visible(page) -> bool:
    selector = (
        "[data-automation-id='SignInWithEmailButton'], "
        "[data-automation-id='createAccountSubmitButton'], "
        "[data-automation-id='signInSubmitButton']"
    )
    return _visible_locator_in_frames(page, selector) is not None


def ensure_workday_account_access(page, company_key: str, apply_url: str) -> tuple[bool, str]:
    """Sign in or create and activate a Workday account, then return to Apply."""
    record = _account_record(company_key)
    _open_email_auth(page, create_account=record is None)
    started = int(time.time())
    maybe_create_account(page, company_key)
    maybe_sign_in(page, company_key)
    page.wait_for_timeout(1200)

    # Earlier releases persisted credentials before Workday's click-filter
    # actually created the remote account. If that stale record cannot sign in,
    # create the missing account with the same saved credentials, then continue
    # through the normal tenant-verified activation flow.
    error = _workday_auth_error(page).lower()
    create = _visible_locator_in_frames(
        page, "[data-automation-id='createAccountLink']"
    )
    stale_local_record = record is not None and create is not None
    if stale_local_record and (
        not error
        or "wrong email address or password" in error
        or "account might be locked" in error
    ):
        create.click(timeout=5000, force=True)
        page.wait_for_timeout(700)
        maybe_create_account(page, company_key)
        _open_email_auth(page, create_account=False)
        maybe_sign_in(page, company_key)
        page.wait_for_timeout(1200)

    if not _verification_required(page):
        if _workday_auth_gate_visible(page):
            detail = _workday_auth_error(page)
            return False, detail or "workday account sign-in did not complete"
        return True, ""

    record = _account_record(company_key)
    created_at = int(record[2]) if record and record[2] else started
    host = urllib.parse.urlparse(apply_url).netloc
    activation_url = fetch_workday_activation_url(company_key, host, created_at)
    if not activation_url:
        return False, "workday account verification email not found"

    page.goto(activation_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    page.goto(apply_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    _open_email_auth(page, create_account=False)
    maybe_sign_in(page, company_key)
    page.wait_for_timeout(2000)
    if _verification_required(page):
        return False, "workday account remains unverified after activation"
    if _workday_auth_gate_visible(page):
        detail = _workday_auth_error(page)
        return False, detail or "workday account sign-in did not complete after activation"
    return True, ""


def _click_workday_submit(scope, automation_id: str) -> None:
    """Click the filter paired with one exact Workday submit button.

    Newer tenants label both account-creation and sign-in filters simply
    ``Submit``. Selecting by that label can hit the wrong form, while clicking
    the hidden button directly is blocked by the filter. Scope the filter to the
    requested button's immediate wrapper instead.
    """
    button = scope.locator(
        f"[data-automation-id={json.dumps(automation_id)}]"
    ).last
    wrapper = button.locator("xpath=..")
    overlay = wrapper.locator(
        ":scope > [data-automation-id='click_filter']"
    ).first
    if overlay.count():
        overlay.click(timeout=5000)
    else:
        button.click(timeout=5000)


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
    _click_workday_submit(scope, "createAccountSubmitButton")
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
    _click_workday_submit(scope, "signInSubmitButton")
    page.wait_for_timeout(4000)


def fill_current_page(page, company_key: str, slug: str) -> None:
    """One extraction + Claude answering + fill pass over the current wizard page."""
    fields = page.evaluate(WD_EXTRACT_JS)
    force_identity(page, fields)
    fields = page.evaluate(WD_EXTRACT_JS)
    unsafe = unsafe_prefilled_fields(fields, company_key)
    if unsafe:
        raise UnsafePrefilledAnswers(unsafe)
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
    # Re-apply deterministic facts even when Workday resumed a saved draft.
    # This corrects stale parser/model values such as a completed Bachelor's
    # claim and ensures list-valued checkbox answers select every approved item.
    grounded = qa.explicit_approved_answers(
        fields,
        key_field="faid",
        company_context=company_key,
    )
    grounded, _ = qa.filter_manual_answers(
        [dict(field, company_context=company_key) for field in fields],
        grounded,
        key_field="faid",
    )
    by_faid = {field["faid"]: field for field in fields}
    for item in grounded:
        field = by_faid.get(item.get("faid"))
        if not field:
            continue
        answer = item.get("answer")
        current = str(field.get("value") or "").strip().lower()
        if isinstance(answer, list) or current != str(answer or "").strip().lower():
            wd_fill(page, field, answer)
            page.wait_for_timeout(250)
    fields = page.evaluate(WD_EXTRACT_JS)
    todo = [f for f in fields if not f["value"]]
    if not todo:
        return
    answers = wd_answers(fields, company_key, slug)
    amap = {a["faid"]: a["answer"] for a in answers if "faid" in a}
    for f in todo:
        if f["faid"] in amap:
            wd_fill(page, f, amap[f["faid"]])
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
        apply_url = url.rstrip("/") + "/apply/autofillWithResume"
        if not (af.count() and af.is_visible()) and \
                _account_scope(page) is page:
            page.goto(apply_url,
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
        if not (af.count() and af.is_visible()) and not saved_draft_wizard_is_active(page):
            account_ok, account_reason = ensure_workday_account_access(
                page, company_key, apply_url
            )
            if not account_ok:
                result.update(ok=True, reason=account_reason,
                              unanswered=["Workday account verification"])
                _shot(page, slug, "account_gate")
                browser.close()
                return result
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
        if not page.locator("[data-automation-id='file-upload-input-ref']").count() and \
                not saved_draft_wizard_is_active(page):
            account_ok, account_reason = ensure_workday_account_access(
                page, company_key, apply_url
            )
            if not account_ok:
                result.update(ok=True, reason=account_reason,
                              unanswered=["Workday account verification"])
                _shot(page, slug, "account_gate")
                browser.close()
                return result
            try:
                if af.count() and af.is_visible():
                    af.click(timeout=8000)
                    page.wait_for_timeout(3000)
            except Exception:
                pass
        up = page.locator("[data-automation-id='file-upload-input-ref']").first
        resume_current = False
        try:
            up.wait_for(state="attached", timeout=20000)
            up.set_input_files(str(resume_pdf))
            page.wait_for_timeout(5000)
            resume_current = True
        except Exception:
            # Workday may resume an authenticated candidate directly into a
            # saved wizard. In that state the initial upload choice no longer
            # exists, but the normal per-page safety gates must still run.
            if not saved_draft_wizard_is_active(page):
                result["reason"] = "resume upload zone never appeared"
                _shot(page, slug, "fail_upload")
                browser.close()
                return result

        # wizard loop
        for page_no in range(MAX_PAGES):
            step = current_step(page)
            body_text = page.inner_text("body").lower()
            if "my experience" in step.lower() and not resume_current:
                if not refresh_saved_resume(page, resume_pdf):
                    result.update(
                        ok=True,
                        reason="needs correction: could not refresh saved resume attachment",
                        unanswered=["Resume/CV attachment"],
                    )
                    _shot(page, slug, "resume_refresh_failed")
                    browser.close()
                    return result
                resume_current = True
                result["resume_refreshed"] = True
            if "review" in step.lower():
                _shot(page, slug, f"review")
                if dry_run:
                    result.update(ok=True, reason="dry run — reached Review, did not submit")
                    browser.close()
                    return result
                sub = page.locator("button:has-text('Submit'), [data-automation-id='pageFooterNextButton']").first
                mark_submit_attempted()
                sub.click(timeout=8000)
                page.wait_for_timeout(6000)
                _shot(page, slug, "submitted")
                body = page.inner_text("body").lower()
                if confirmation_observed(body, page.url):
                    result.update(ok=True, submitted=True, reason="confirmed")
                else:
                    mark_unconfirmed(result)
                browser.close()
                return result

            # Fill-and-advance with up to 3 passes per step. Conditional questions
            # appear after earlier answers, so each pass re-extracts + re-harvests.
            advanced = False
            for fill_pass in range(3):
                try:
                    fill_current_page(page, company_key, slug)
                except UnsafePrefilledAnswers as exc:
                    result.update(
                        ok=True,
                        reason=f"needs correction: unsafe prefilled answers: {exc.labels}",
                        unanswered=exc.labels,
                    )
                    _shot(page, slug, "unsafe_prefilled")
                    browser.close()
                    return result
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
                result["unanswered"] = [f["label"] for f in page.evaluate(WD_EXTRACT_JS)
                                        if f["required"] and not f["value"]][:10]
                if result["unanswered"]:
                    result.update(
                        ok=True,
                        reason=f"needs answers: {result['unanswered']}",
                    )
                else:
                    result["reason"] = f"stuck on '{step}': {errs[:3]}"
                _shot(page, slug, "stuck")
                browser.close()
                return result
        result["reason"] = "wizard exceeded max pages"
        browser.close()
    return result


if __name__ == "__main__":
    print(apply_workday(sys.argv[1], Path(sys.argv[2]), "wd_test", dry_run=True))
