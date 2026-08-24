"""Oracle Recruiting Cloud pre-submit application adapter.

Task 3 intentionally stops at the submit boundary. It may click the visible
Apply entry point, fill the anonymous pre-submit form, and capture a local
filled-form screenshot, but it never clicks Submit.
"""
from __future__ import annotations

import re
import sys
import time
from email.utils import getaddresses
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

try:
    from artifacts import safe_screenshot
    import qa
    import stealth
    from submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
    from timeouts import configure_page
except ModuleNotFoundError:  # package import from tests
    from apply.artifacts import safe_screenshot
    from apply import qa, stealth
    from apply.submission_state import confirmation_observed, mark_submit_attempted, mark_unconfirmed
    from apply.timeouts import configure_page

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
SHOTS = ROOT / "out" / "screenshots"

APPLY_SELECTORS = (
    "button:has-text('Apply Now')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply')",
    "[data-bind*='apply'][role='button']",
)

APPLY_WAIT_TIMEOUT_MS = 10000
APPLY_POLL_INTERVAL_MS = 500
ORACLE_IDENTITY_SUBJECT = "Please confirm your identity"
ORACLE_IDENTITY_BODY_PHRASE = "confirm your identity using the one-time passcode below:"
ORACLE_IDENTITY_CLOCK_TOLERANCE_MS = 2000

SUBMIT_SELECTORS = (
    "button:text-is('Submit')",
    "input[type='submit'][value='Submit']",
)

CLOSED_PATTERNS = (
    "job is no longer available",
    "posting is no longer available",
    "job posting is no longer available",
    "this job is no longer accepting applications",
)

ACCOUNT_PATTERNS = (
    "sign in to apply",
    "signin to apply",
    "create an account to continue",
    "must create an account",
    "account required",
    "login to apply",
    "log in to apply",
)

CAPTCHA_PATTERNS = (
    "recaptcha",
    "reCAPTCHA".lower(),
    "hcaptcha",
    "datadome",
    "verify you are human",
    "captcha-delivery.com",
)

REQUIRED_EMPTY_JS = r"""
() => {
  const labelFor = (el) => {
    let label = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || '';
    if (!label) {
      const ref = el.getAttribute('aria-labelledby');
      if (ref) label = ref.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
    }
    if (!label) {
      const wrap = el.closest('fieldset, [role=group], div[class*=question], div[class*=field], label');
      label = wrap?.querySelector('legend, label, [class*=label], [class*=question]')?.innerText || '';
      if (!label && wrap) label = (wrap.innerText || '').split('\n')[0] || '';
    }
    return (label || el.name || el.id || 'unknown').replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  const bad = [];
  document.querySelectorAll('[aria-required="true"], [required]').forEach(el => {
    if (el.getAttribute('aria-hidden') === 'true' || el.type === 'hidden' || el.type === 'file') return;
    if (el.offsetParent === null) return;
    if (el.type === 'checkbox' || el.type === 'radio') {
      const group = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
      if (group.some(x => x.checked)) return;
    } else if (String(el.value || '').trim()) {
      return;
    }
    bad.push(labelFor(el));
  });
  return [...new Set(bad)].slice(0, 6);
}
"""


def _manual(reason: str, unanswered: list[str] | None = None, *, retryable: bool = False) -> dict:
    return {
        "ok": True,
        "submitted": False,
        "outcome": "manual",
        "reason": reason,
        "unanswered": unanswered or [],
        "retryable": retryable,
        "click_attempted": False,
        "submission_uncertain": False,
    }


def _record_filled_screenshot(result: dict, page, slug: str) -> None:
    shot = safe_screenshot(page, slug, "filled", root=SHOTS)
    if shot:
        result.setdefault("artifact_refs", {})["filled_form_screenshot"] = shot


def _body_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _closed_reason(body: str) -> str | None:
    lower = body.lower()
    for marker in CLOSED_PATTERNS:
        if marker in lower:
            return marker
    return None


def _has_account_gate(body: str) -> bool:
    lower = body.lower()
    return any(marker in lower for marker in ACCOUNT_PATTERNS)


def _has_captcha_gate(page, body: str) -> bool:
    lower = body.lower()
    if any(marker in lower for marker in CAPTCHA_PATTERNS):
        return True
    selectors = (
        "iframe[src*='recaptcha' i]",
        "iframe[title*='recaptcha' i]",
        "iframe[src*='hcaptcha' i]",
        "iframe[title*='hcaptcha' i]",
        "iframe[src*='captcha-delivery.com' i]",
        "iframe[title*='DataDome' i]",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _find_visible_apply(page):
    for selector in APPLY_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _wait_for_visible_apply(page, *, timeout_ms: int = APPLY_WAIT_TIMEOUT_MS, poll_ms: int = APPLY_POLL_INTERVAL_MS):
    elapsed_ms = 0
    while elapsed_ms <= timeout_ms:
        apply_button = _find_visible_apply(page)
        if apply_button is not None:
            return apply_button
        if elapsed_ms >= timeout_ms:
            break
        interval = min(poll_ms, timeout_ms - elapsed_ms)
        try:
            page.wait_for_timeout(interval)
        except Exception:
            break
        elapsed_ms += interval
    return None


def _find_exact_visible_submit(page):
    candidates = []
    for selector in SUBMIT_SELECTORS:
        try:
            loc = page.locator(selector)
            count = loc.count()
            if count == 1:
                if loc.is_visible():
                    candidates.append(loc)
            elif count > 1:
                for index in range(count):
                    cand = loc.nth(index)
                    if cand.is_visible():
                        candidates.append(cand)
        except Exception:
            continue
    if len(candidates) == 1:
        return candidates[0]
    return None


def _single_visible_exact_button(page, text: str):
    try:
        loc = page.get_by_role("button", name=text, exact=True)
        visible = []
        for index in range(loc.count()):
            cand = loc.nth(index)
            if cand.is_visible():
                visible.append(cand)
        if len(visible) == 1:
            return visible[0]
    except Exception:
        return None
    return None


def _visible_exact_button_count(page, text: str) -> int:
    try:
        loc = page.get_by_role("button", name=text, exact=True)
        visible = 0
        for index in range(loc.count()):
            if loc.nth(index).is_visible():
                visible += 1
        return visible
    except Exception:
        return 0


def _message_headers(full: dict) -> dict[str, str]:
    headers = ((full.get("payload") or {}).get("headers") or []) if isinstance(full, dict) else []
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers if isinstance(h, dict)}


def _message_matches_profile_address(headers: dict[str, str]) -> bool:
    profile_email = str(PROFILE.get("email", "")).strip().lower()
    if not profile_email:
        return False
    raw_recipients = [
        value
        for name in ("to", "cc", "delivered-to")
        if (value := headers.get(name, "").strip())
    ]
    addresses = [addr.lower() for _name, addr in getaddresses(raw_recipients)]
    return profile_email in addresses


def _extract_oracle_identity_code(text: str) -> str | None:
    if ORACLE_IDENTITY_BODY_PHRASE not in text:
        return None
    codes = re.findall(r"\b\d{6}\b", text)
    if len(codes) != 1:
        return None
    return codes[0]


def _fetch_oracle_identity_code(requested_at_ms: int, timeout_s: int = 60) -> str | None:
    notify = ROOT / "notify"
    if str(notify) not in sys.path:
        sys.path.insert(0, str(notify))
    try:
        import mailer
    except Exception:
        return None

    deadline = time.time() + max(timeout_s, 0)
    query = "subject:(Please confirm your identity) newer_than:1h"
    first = True
    while first or time.time() < deadline:
        first = False
        try:
            data = mailer._call(f"/messages?q={query.replace(' ', '%20')}&maxResults=10")
            messages = []
            for msg in data.get("messages", [])[:10]:
                full = mailer._call(f"/messages/{msg['id']}?format=full")
                messages.append(full)
            messages.sort(key=lambda m: int(m.get("internalDate", 0) or 0), reverse=True)
            for full in messages[:5]:
                internal_ms = int(full.get("internalDate", 0) or 0)
                if internal_ms < requested_at_ms - ORACLE_IDENTITY_CLOCK_TOLERANCE_MS:
                    continue
                headers = _message_headers(full)
                if headers.get("subject") != ORACLE_IDENTITY_SUBJECT:
                    continue
                if not _message_matches_profile_address(headers):
                    continue
                text = mailer.extract_plain(full) or full.get("snippet", "") or ""
                code = _extract_oracle_identity_code(text)
                if code:
                    return code
            if timeout_s <= 0:
                break
        except Exception:
            return None
        time.sleep(3)
    return None


def _is_oracle_identity_gate(page) -> bool:
    page_url = getattr(page, "url", "")
    if not (page_url.endswith("/apply/email") or page_url.endswith("/apply/email/")):
        return False
    body = _body_text(page)
    return "Confirm Your Identity" in body and "The verification code was sent to this email address:" in body


def _find_identity_pin_inputs(page):
    pins = []
    for digit in range(1, 7):
        selector = f"#pin-code-{digit}"
        loc = page.locator(selector)
        if loc.count() != 1 or not loc.is_visible():
            return None
        if (loc.get_attribute("type") or "") != "number":
            return None
        if (loc.get_attribute("autocomplete") or "") != "off":
            return None
        expected_label = f"Enter verification code digit {digit} of six."
        if (loc.get_attribute("aria-label") or "") != expected_label:
            return None
        pins.append(loc)
    return pins


def _handle_oracle_identity_gate(page, requested_at_ms: int) -> dict | None:
    if not _is_oracle_identity_gate(page):
        return None
    code = _fetch_oracle_identity_code(requested_at_ms, timeout_s=60)
    if not code or not re.fullmatch(r"\d{6}", code):
        return _manual("Oracle identity verification code was absent, ambiguous, malformed, stale, or unavailable")
    pins = _find_identity_pin_inputs(page)
    if pins is None:
        return _manual("Oracle identity verification controls were missing or ambiguous")
    verify = _single_visible_exact_button(page, "Verify")
    if verify is None:
        return _manual("Oracle identity verification Verify control was missing or ambiguous")
    try:
        for loc, digit in zip(pins, code):
            loc.fill(digit)
    except Exception as exc:
        return _manual(f"Oracle identity verification pin fill failed: {type(exc).__name__}: {exc}")
    try:
        verify.click(timeout=5000)
    except Exception as exc:
        return _manual(f"Oracle identity verification Verify click failed: {type(exc).__name__}: {exc}")
    for _ in range(10):
        try:
            page.wait_for_timeout(500)
        except Exception:
            break
        body = _body_text(page)
        if (
            "Too Many Attempts. Try Again Later." in body
            and "You reached the maximum number of attempts. Try again in 30 minutes." in body
        ):
            return _manual(
                "Oracle identity verification rate limited; retry after 30 minutes",
                ["Oracle identity verification"],
                retryable=True,
            )
        if _has_account_gate(body):
            return _manual("Oracle account required for application", ["Oracle account required"])
        if _has_captcha_gate(page, body):
            return _manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"])
        if _find_resume_input(page) is not None:
            return None
    if _is_oracle_identity_gate(page):
        return _manual("Oracle identity verification was unchanged and did not advance to resume upload")
    return _manual("Oracle identity verification did not advance to resume upload")


def _check_oracle_legal_disclaimer(page, legal) -> bool:
    try:
        legal.check(force=True, timeout=5000)
        return legal.is_checked()
    except Exception:
        try:
            label = page.locator("label[for='legal-disclaimer-checkbox']")
            if label.count() != 1 or not label.is_visible():
                return False
            proxy = page.locator("label[for='legal-disclaimer-checkbox'] .apply-flow-input-checkbox__button")
            if proxy.count() != 1 or not proxy.is_visible():
                return False
            proxy.click(timeout=5000)
            return legal.is_checked()
        except Exception:
            return False


def _handle_anonymous_email_gate(page) -> dict | None:
    body = _body_text(page)
    page_url = getattr(page, "url", "")
    if not page_url.endswith("/apply/email") and not page_url.endswith("/apply/email/"):
        return None
    if "You don't need to have an account" not in body:
        return None
    if "Email Address" not in body or "I agree with the terms and conditions" not in body:
        return _manual("unsupported Oracle anonymous email gate: incomplete gate shape")

    email = page.locator("input[type=email][name='primary-email']")
    if email.count() != 1 or not email.is_visible():
        return _manual("unsupported Oracle anonymous email gate: incomplete gate shape")
    legal = page.locator("#legal-disclaimer-checkbox")
    if legal.count() != 1:
        return _manual("unsupported Oracle anonymous email gate: incomplete gate shape")
    next_button = _single_visible_exact_button(page, "Next")
    if next_button is None:
        return _manual("unsupported Oracle anonymous email gate: incomplete or ambiguous Next control")

    email.fill(str(PROFILE.get("email", "")))
    if not _check_oracle_legal_disclaimer(page, legal):
        return _manual("unsupported Oracle anonymous email gate: incomplete gate shape")
    requested_at_ms = int(time.time() * 1000)
    next_button.click(timeout=5000)
    for _ in range(10):
        try:
            page.wait_for_timeout(500)
        except Exception:
            break
        body = _body_text(page)
        if _has_account_gate(body):
            return _manual("Oracle account required for application", ["Oracle account required"])
        if _has_captcha_gate(page, body):
            return _manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"])
        identity_result = _handle_oracle_identity_gate(page, requested_at_ms)
        if identity_result is not None:
            return identity_result
        if _find_resume_input(page) is not None:
            return None
    return None


def _uncertain_result(result: dict, reason: str) -> dict:
    result.update(ok=False, submitted=False, reason=reason)
    mark_unconfirmed(result)
    return result


def _find_resume_input(page):
    best = None
    best_score = -1
    try:
        files = page.locator("input[type=file]")
        for index in range(files.count()):
            cand = files.nth(index)
            accept = (cand.get_attribute("accept") or "").lower()
            near = ""
            try:
                near = cand.evaluate(
                    "el => (el.labels?.[0]?.innerText || el.closest('section, div, form')?.innerText || '').slice(0, 300)"
                ) or ""
            except Exception:
                near = ""
            if not re.search(r"\b(resume|cv|curriculum[- ]vitae)\b", near.lower()):
                continue
            score = 1 if ("pdf" in accept or "msword" in accept or "document" in accept) else 0
            if score > best_score:
                best = cand
                best_score = score
    except Exception:
        best = None
    if best is not None:
        return best
    for label in ("Resume", "CV", "Curriculum Vitae"):
        try:
            cand = page.get_by_label(label, exact=False).first
            if cand.count() and cand.is_visible():
                return cand
        except Exception:
            continue
    return None


def _fill_if_visible(page, labels: tuple[str, ...], value: str) -> None:
    if value is None:
        return
    for label in labels:
        try:
            loc = page.get_by_label(label, exact=False).first
            if loc.count() and loc.is_visible():
                try:
                    if str(loc.input_value() or "").strip():
                        return
                except Exception:
                    pass
                try:
                    loc.fill("")
                except Exception:
                    pass
                loc.fill(str(value))
                return
        except Exception:
            continue


def _normal_control_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value).lower())).strip()


def _owned_oracle_answer(answer: dict) -> bool:
    return _owned_oracle_control(answer)


def _owned_oracle_control(control: dict) -> bool:
    label = _normal_control_text(control.get("label") or "")
    key = _normal_control_text(control.get("id_or_name") or control.get("id") or control.get("name") or "")
    owned = {
        "phone", "phone number", "mobile", "mobile phone", "mobile number",
        "country",
        "address line 1", "address line 2", "address line 3",
        "street", "street address",
        "zip", "zipcode", "zip code", "postal", "postal code",
        "city", "state", "province", "state province", "state or province",
        "county",
    }
    return label in owned or key in owned


def _national_phone_digits(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _control_attr(loc, name: str) -> str:
    try:
        return str(loc.get_attribute(name) or "")
    except Exception:
        return ""


def _has_control_attr(loc, name: str) -> bool:
    try:
        if hasattr(loc, "attrs") and name in getattr(loc, "attrs"):
            return True
        return loc.get_attribute(name) is not None
    except Exception:
        return False


def _is_fillable_text_control(loc) -> bool:
    try:
        if not loc.is_visible():
            return False
    except Exception:
        return False
    try:
        tag_name = str(loc.evaluate("element => element.tagName") or "").lower()
        if tag_name == "select":
            return False
    except Exception:
        pass
    attrs = {name: _control_attr(loc, name).lower() for name in ("type", "role", "aria-readonly", "aria-disabled", "hidden")}
    if attrs["role"] in {"combobox", "listbox", "button"}:
        return False
    if attrs["aria-readonly"] == "true" or attrs["aria-disabled"] == "true" or attrs["hidden"]:
        return False
    if _has_control_attr(loc, "readonly") or _has_control_attr(loc, "disabled"):
        return False
    input_type = attrs["type"] or "text"
    return input_type in {"text", "tel", "", "search", "email", "number"}


def _fill_phone_if_visible(page, labels: tuple[str, ...], value: str) -> None:
    digits = _national_phone_digits(value)
    if not digits:
        return
    candidates = []
    for label in labels:
        try:
            locs = page.get_by_label(label, exact=False)
            for index in range(locs.count()):
                loc = locs.first if locs.count() == 1 else locs.nth(index)
                if not _is_fillable_text_control(loc):
                    continue
                if "country" in _control_attr(loc, "aria-label").lower():
                    continue
                try:
                    if str(loc.input_value() or "").strip():
                        return
                except Exception:
                    pass
                candidates.append(loc)
            if candidates:
                break
        except Exception:
            continue
    if len(candidates) == 1:
        candidates[0].fill(digits)


def _profile_street() -> str:
    for source_name in ("location", "address"):
        source = PROFILE.get(source_name) or {}
        for key in ("street", "address_line_1", "address1", "line1"):
            if str(source.get(key) or "").strip():
                return str(source.get(key)).strip()
    return ""


def _fill_oracle_address_line1(page) -> bool:
    street = _profile_street()
    if not street:
        return False
    try:
        fields = page.get_by_label("Address Line 1", exact=True)
        if fields.count() != 1:
            return False
        field = fields.nth(0)
        if not field.is_visible():
            return False
        if str(field.input_value() or "").strip():
            return False
    except Exception:
        return False
    committed = False
    attempted = False
    try:
        try:
            attempted = True
            field.press_sequentially(street, delay=20)
        except Exception:
            return False
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass
        matches = []
        try:
            options = page.locator("[role='option'], [role=option], [role='menuitem']")
            target = _normal_control_text(street)
            for index in range(options.count()):
                option = options.nth(index)
                if not option.is_visible():
                    continue
                text = option.inner_text(timeout=500)
                if target and target in _normal_control_text(text):
                    matches.append(option)
        except Exception:
            return False
        if len(matches) != 1:
            return False
        try:
            matches[0].click(timeout=1000)
        except Exception:
            return False
        try:
            committed_value = str(field.input_value() or "").strip()
            committed = bool(committed_value) and _normal_control_text(street) in _normal_control_text(committed_value)
        except Exception:
            return False
        return committed
    except Exception:
        return False
    finally:
        try:
            if attempted and not committed:
                field.fill("")
        except Exception:
            pass


def _fill_basics(page) -> None:
    p = PROFILE
    name = p.get("name") or {}
    links = p.get("links") or {}
    _fill_if_visible(page, ("First name", "First Name", "Given name"), name.get("first", ""))
    _fill_if_visible(page, ("Last name", "Last Name", "Family name", "Surname"), name.get("last", ""))
    _fill_if_visible(page, ("Email", "Email address"), p.get("email", ""))
    _fill_phone_if_visible(page, ("Phone", "Phone number", "Mobile"), str(p.get("phone", "")))
    _fill_if_visible(page, ("LinkedIn", "LinkedIn profile"), links.get("linkedin", ""))
    _fill_if_visible(page, ("GitHub", "Portfolio", "Website"), links.get("github") or links.get("website") or "")


def _run_shared_qa_passes(page, slug: str, url: str) -> tuple[list[str], list[str]]:
    filled: list[str] = []
    failed: list[str] = []
    answers = None
    for qa_pass in range(3):
        controls = page.evaluate(qa.EXTRACT_JS)
        shared_controls = [control for control in controls if not _owned_oracle_control(control)]
        if answers is None:
            answers = qa.get_answers(shared_controls, context={"slug": slug, "url": url})
        by_key = {}
        by_label = {}
        for control in shared_controls:
            if control.get("id"):
                by_key[control.get("id")] = control
            if control.get("name"):
                by_key.setdefault(control.get("name"), control)
            if control.get("label"):
                by_label.setdefault(str(control.get("label")).lower().strip(), control)

        todo = []
        for answer in answers:
            control = by_key.get(answer.get("id_or_name"))
            if control is None:
                label = str(answer.get("label") or answer.get("id_or_name") or "").lower().strip()
                control = by_label.get(label)
            if control is None and _owned_oracle_answer(answer):
                continue
            if control is None or (not control.get("value") and not control.get("chosen")):
                todo.append(answer)
        newly_filled, failed = qa.fill_answers(page, shared_controls, todo)
        failed = [item for item in failed if not _owned_oracle_answer({"label": item, "id_or_name": item})]
        filled.extend(newly_filled)
        try:
            page.wait_for_timeout(600)
        except Exception:
            pass
    return sorted(set(filled)), failed


def _page_signature(page) -> tuple[str, str]:
    return (str(getattr(page, "url", "")), _body_text(page))


def _merge_unique(existing: list[str], new: list[str]) -> list[str]:
    merged = list(existing)
    for item in new:
        if item not in merged:
            merged.append(item)
    return merged


def apply_oraclecloud(url: str, resume_pdf: Path, slug: str, dry_run: bool = True) -> dict:
    result = {"ok": False, "submitted": False, "reason": "", "unanswered": []}
    with sync_playwright() as pw:
        browser, ctx = stealth.launch_stealth_context(pw)
        page = configure_page(ctx.new_page())
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass

            body = _body_text(page)
            closed = _closed_reason(body)
            if closed:
                result.update(ok=True, outcome="stale", reason=closed, unanswered=[])
                return result
            if _has_account_gate(body):
                return _manual("Oracle account required for application", ["Oracle account required"])
            if _has_captcha_gate(page, body):
                return _manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"])

            apply_button = _wait_for_visible_apply(page)
            if apply_button is None:
                return _manual("unsupported Oracle tenant variant: apply control not found")
            apply_button.scroll_into_view_if_needed()
            apply_button.click(timeout=5000)
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

            body = _body_text(page)
            if _has_account_gate(body):
                return _manual("Oracle account required for application", ["Oracle account required"])
            if _has_captcha_gate(page, body):
                return _manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"])

            gate_result = _handle_anonymous_email_gate(page)
            if gate_result is not None:
                return gate_result

            resume_uploaded = False
            result["qa_filled"] = []
            result["qa_failed"] = []
            for page_index in range(1, 5):
                if page_index == 1:
                    resume_input = _find_resume_input(page)
                    if resume_input is None:
                        result.update(_manual("resume upload not found", ["Resume"]))
                        _record_filled_screenshot(result, page, slug)
                        return result
                    resume_input.set_input_files(str(resume_pdf))
                    resume_uploaded = True
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass

                _fill_basics(page)
                _fill_oracle_address_line1(page)
                qa_filled, qa_failed = _run_shared_qa_passes(page, slug, url)
                result["qa_filled"] = _merge_unique(result["qa_filled"], qa_filled)
                result["qa_failed"] = _merge_unique(result["qa_failed"], qa_failed)

                body = _body_text(page)
                closed = _closed_reason(body)
                if closed:
                    result.update(ok=True, outcome="stale", reason=closed, unanswered=[])
                    _record_filled_screenshot(result, page, slug)
                    return result
                if _has_account_gate(body):
                    result.update(_manual("Oracle account required for application", ["Oracle account required"]))
                    _record_filled_screenshot(result, page, slug)
                    return result
                if _has_captcha_gate(page, body):
                    result.update(_manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"]))
                    _record_filled_screenshot(result, page, slug)
                    return result

                required_empty = page.evaluate(REQUIRED_EMPTY_JS)
                unanswered = _merge_unique(list(required_empty or []), qa_failed)
                result["unanswered"] = unanswered
                _record_filled_screenshot(result, page, slug)
                if unanswered:
                    result.update(_manual(f"needs answers: {unanswered[:6]}", unanswered[:6]))
                    _record_filled_screenshot(result, page, slug)
                    return result

                submit_button = _find_exact_visible_submit(page)
                if submit_button is not None:
                    if dry_run:
                        result.update(ok=True, submitted=False, reason="dry run - did not submit", unanswered=[])
                        return result
                    break

                if page_index == 4:
                    result.update(_manual("unsupported Oracle tenant variant: more than four application pages required"))
                    return result

                next_button = _single_visible_exact_button(page, "Next")
                if next_button is None:
                    if _visible_exact_button_count(page, "Next") == 0:
                        result.update(_manual("unsupported Oracle tenant variant: exact visible Submit control not found"))
                    else:
                        result.update(_manual("unsupported Oracle tenant variant: exact visible Next control ambiguous"))
                    return result
                before = _page_signature(page)
                next_button.click(timeout=5000)
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                body = _body_text(page)
                closed = _closed_reason(body)
                if closed:
                    result.update(ok=True, outcome="stale", reason=closed, unanswered=[])
                    _record_filled_screenshot(result, page, slug)
                    return result
                if _has_account_gate(body):
                    result.update(_manual("Oracle account required for application", ["Oracle account required"]))
                    _record_filled_screenshot(result, page, slug)
                    return result
                if _has_captcha_gate(page, body):
                    result.update(_manual("Oracle CAPTCHA requires manual completion", ["Oracle CAPTCHA"]))
                    _record_filled_screenshot(result, page, slug)
                    return result
                if _page_signature(page) == before:
                    result.update(_manual("Oracle Next transition did not advance the application page"))
                    return result

            else:
                result.update(_manual("unsupported Oracle tenant variant: more than four application pages required"))
                return result

            if not resume_uploaded:
                result.update(_manual("resume upload not found", ["Resume"]))
                return result

            try:
                mark_submit_attempted()
                submit_button.click(timeout=5000)
                try:
                    page.wait_for_timeout(5000)
                except Exception as exc:
                    return _uncertain_result(result, f"submit confirmation uncertain: {type(exc).__name__}: {exc}")
                body = _body_text(page)
                page_url = getattr(page, "url", "")
                if confirmation_observed(body, page_url):
                    result.update(
                        ok=True,
                        submitted=True,
                        outcome="submitted",
                        reason="confirmed",
                        unanswered=[],
                        retryable=False,
                        click_attempted=True,
                        submission_uncertain=False,
                    )
                    return result
                return _uncertain_result(result, "submit confirmation was not observed")
            except Exception as exc:
                return _uncertain_result(result, f"submit click uncertain: {type(exc).__name__}: {exc}")
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    print(apply_oraclecloud(sys.argv[1], Path(sys.argv[2]), "oracle_test", dry_run=True))
