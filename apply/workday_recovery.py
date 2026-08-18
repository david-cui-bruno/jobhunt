"""Workday candidate-account password recovery.

When a tenant rejects the stored password ("wrong email address or password",
"account might be locked", or the generic sign-in-did-not-complete), drive the
tenant's Forgot Password flow, pull the reset email from Gmail, set a fresh
generated password, and store it in wd_accounts.

Observed structure (coreandmain 2026-08-18, standard across tenants):
  login page -> [SignInWithEmailButton] -> sign-in form with
  [forgotPasswordLink] -> email + [resetPasswordButton] -> Workday emails
  "Reset your password for your candidate account" from
  <tenant>@otp.workday.com containing https://<host>/<site>/passwordreset/<token>
  -> that page renders password/verifyPassword + [resetPasswordButton].

Safety:
  - The new password is stored in wd_accounts BEFORE the reset form is
    submitted, so a crash can never orphan a password Workday accepted.
  - One recovery attempt per tenant per day (wd_recovery_attempts guard);
    callers must claim via claim_daily_recovery() before acting.
  - Failure screenshots land in out/screenshots like the adapter's.
"""
from __future__ import annotations

import html
import re
import secrets
import sqlite3
import string
import sys
import time
import urllib.parse
from pathlib import Path

import workday as wd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "tracker.db"

PASSWORD_LENGTH = 20
# Symbols Workday's default policy accepts everywhere; avoids quoting hazards.
PASSWORD_SYMBOLS = "!@#$%*+-"
RESET_PATH = re.compile(r"/passwordreset/[^/?#]+")
# Poll budget for the reset email. Workday sends within seconds; keep the
# ceiling under the submitter's per-posting deadline (300s) so recovery plus
# the remaining wizard still fits.
POLL_TIMEOUT_S = 150
POLL_INTERVAL_S = 6


# ---------- password generation / storage ----------

def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A password meeting Workday's rules: upper, lower, digit, symbol."""
    groups = (string.ascii_uppercase, string.ascii_lowercase,
              string.digits, PASSWORD_SYMBOLS)
    chars = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    chars += [secrets.choice(alphabet) for _ in range(max(length, len(groups)) - len(groups))]
    for i in range(len(chars) - 1, 0, -1):  # secrets-driven Fisher-Yates
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wd_accounts "
        "(tenant TEXT PRIMARY KEY, email TEXT, password TEXT, created_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wd_recovery_attempts "
        "(tenant TEXT, day TEXT, attempted_at INTEGER, outcome TEXT, "
        "PRIMARY KEY (tenant, day))"
    )


def store_password(tenant: str, email: str, password: str) -> None:
    """Persist credentials; keeps created_at on update (activation email search
    keys off the original creation time)."""
    conn = sqlite3.connect(DB)
    try:
        _ensure_tables(conn)
        updated = conn.execute(
            "UPDATE wd_accounts SET email=?, password=? WHERE tenant=?",
            (email, password, tenant),
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO wd_accounts VALUES (?,?,?,?)",
                (tenant, email, password, int(time.time())),
            )
        conn.commit()
    finally:
        conn.close()


def account_email(tenant: str) -> str:
    conn = sqlite3.connect(DB)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT email FROM wd_accounts WHERE tenant=?", (tenant,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0] if row and row[0] else None) or wd.PROFILE["email"]


# ---------- once-per-day guard ----------

def _today(day: str | None) -> str:
    return day or time.strftime("%Y-%m-%d", time.gmtime())


def claim_daily_recovery(tenant: str, day: str | None = None) -> bool:
    """Atomically claim today's single recovery attempt for a tenant."""
    conn = sqlite3.connect(DB)
    try:
        _ensure_tables(conn)
        claimed = conn.execute(
            "INSERT OR IGNORE INTO wd_recovery_attempts "
            "(tenant, day, attempted_at, outcome) VALUES (?,?,?,?)",
            (tenant, _today(day), int(time.time()), "attempting"),
        ).rowcount == 1
        conn.commit()
        return claimed
    finally:
        conn.close()


def record_recovery_outcome(tenant: str, outcome: str,
                            day: str | None = None) -> None:
    conn = sqlite3.connect(DB)
    try:
        _ensure_tables(conn)
        conn.execute(
            "UPDATE wd_recovery_attempts SET outcome=? WHERE tenant=? AND day=?",
            (str(outcome)[:300], tenant, _today(day)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- reset email parsing ----------

def _message_bodies(part: dict):
    import base64
    data = part.get("body", {}).get("data")
    if data:
        try:
            padded = data + "=" * (-len(data) % 4)
            yield base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
        except Exception:
            pass
    for child in part.get("parts", []) or []:
        yield from _message_bodies(child)


def reset_url_from_message(message: dict, expected_host: str) -> str | None:
    """Extract only a same-tenant Workday password-reset URL."""
    for body in _message_bodies(message.get("payload", {})):
        for raw in re.findall(r"https?://[^\s<>\"']+", html.unescape(body)):
            url = html.unescape(raw).rstrip(".,);]")
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc.lower() != expected_host.lower():
                continue
            if RESET_PATH.search(parsed.path):
                return url
    return None


def security_code_from_message(message: dict) -> str | None:
    """Older tenants email a numeric security code instead of a link."""
    for body in _message_bodies(message.get("payload", {})):
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        m = (re.search(r"security code[^0-9]{0,60}(\d{4,8})", text, re.I)
             or re.search(r"(\d{4,8})[^0-9]{0,60}security code", text, re.I))
        if m:
            return m.group(1)
    return None


def _from_matches_tenant(message: dict, tenant: str) -> bool:
    headers = {h["name"].lower(): h["value"]
               for h in message.get("payload", {}).get("headers", [])}
    sender = str(headers.get("from", "")).lower()
    key = re.sub(r"[^a-z0-9]", "", tenant.lower())
    return bool(key) and (key in re.sub(r"[^a-z0-9]", "", sender)
                          or "workday" in sender)


def fetch_reset_email(tenant: str, expected_host: str, not_before: int,
                      timeout_s: int = POLL_TIMEOUT_S) -> dict | None:
    """Poll Gmail for this tenant's reset email; newest first, host-bound.

    Returns {"url": ...} for the link variant or {"code": ...} for the older
    security-code variant, else None.
    """
    notify = ROOT / "notify"
    if str(notify) not in sys.path:
        sys.path.insert(0, str(notify))
    import mailer

    after = max(0, int(not_before) - 60)
    query = urllib.parse.quote(
        f'in:anywhere after:{after} (subject:"reset your password" '
        f'OR subject:"password reset" OR subject:"security code")'
    )
    deadline = time.monotonic() + max(timeout_s, POLL_INTERVAL_S)
    while True:
        try:
            data = mailer._call(f"/messages?q={query}&maxResults=10")
        except Exception:
            data = {}
        for item in data.get("messages", []) or []:  # newest first
            try:
                message = mailer._call(f"/messages/{item['id']}?format=full")
            except Exception:
                continue
            internal = int(message.get("internalDate", 0)) // 1000
            if internal and internal < after:
                continue
            url = reset_url_from_message(message, expected_host)
            if url:
                return {"url": url}
            if _from_matches_tenant(message, tenant):
                code = security_code_from_message(message)
                if code:
                    return {"code": code}
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_S)


# ---------- browser flow ----------

def login_url_from(url: str) -> str:
    """The tenant career-site login route for any posting/apply URL."""
    parsed = urllib.parse.urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if segments and len(segments) > 1 and re.fullmatch(
            r"[a-z]{2}(-[A-Za-z]{2,4})?", segments[0]):
        site = segments[:2]  # locale prefix, e.g. en-US/<site>
    else:
        site = segments[:1]
    return f"{parsed.scheme}://{parsed.netloc}/" + "/".join(site + ["login"])


def _find_frame_locator(page, selector: str):
    """(frame, locator) of the newest visible match across frames."""
    for frame in reversed(page.frames):
        try:
            loc = frame.locator(selector).last
            if loc.count() and loc.is_visible():
                return frame, loc
        except Exception:
            continue
    return None, None


def _dismiss_cookie_banner(page) -> None:
    banner = page.locator(
        "[data-automation-id='legalNoticeAcceptButton'], "
        "#onetrust-accept-btn-handler"
    ).first
    try:
        if banner.count() and banner.is_visible():
            banner.click(timeout=3000)
            page.wait_for_timeout(800)
    except Exception:
        pass


def _fail(page, tenant: str, step: str, detail: str = "") -> tuple[bool, str]:
    try:
        wd._shot(page, f"wdrecovery_{tenant}", re.sub(r"\W+", "_", step))
    except Exception:
        pass
    reason = f"workday recovery failed at {step}"
    if detail:
        reason += f": {detail}"
    return False, reason


def recover_account(tenant: str, base_url: str, page=None,
                    email: str | None = None,
                    poll_timeout_s: int = POLL_TIMEOUT_S) -> tuple[bool, str]:
    """Reset the tenant's candidate-account password; returns (ok, reason).

    With no page supplied, launches its own headless browser using the same
    pattern as apply_workday.
    """
    if page is not None:
        return _recover_with_page(page, tenant, base_url, email, poll_timeout_s)
    from playwright.sync_api import sync_playwright
    from timeouts import configure_page
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 1400})
        fresh = configure_page(ctx.new_page())
        try:
            return _recover_with_page(fresh, tenant, base_url, email,
                                      poll_timeout_s)
        finally:
            browser.close()


def _recover_with_page(page, tenant: str, base_url: str,
                       email: str | None,
                       poll_timeout_s: int) -> tuple[bool, str]:
    login_url = login_url_from(base_url)
    address = email or account_email(tenant)
    host = urllib.parse.urlparse(base_url).netloc

    # 1. Open the sign-in page and reach the email form.
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
    except Exception as exc:
        return _fail(page, tenant, "open sign-in page", type(exc).__name__)
    _dismiss_cookie_banner(page)
    _, choice = _find_frame_locator(
        page, "[data-automation-id='SignInWithEmailButton']")
    if choice is not None:
        try:
            choice.click(timeout=5000)
        except Exception:
            choice.click(timeout=5000, force=True)
        page.wait_for_timeout(1500)

    # 2. Forgot Password link.
    _, forgot = _find_frame_locator(
        page,
        "[data-automation-id='forgotPasswordLink'], "
        "button:has-text('Forgot'), a:has-text('Forgot'), "
        "button:has-text('Reset Password'), a:has-text('Reset Password')",
    )
    if forgot is None:
        return _fail(page, tenant, "forgot-password link")
    try:
        forgot.click(timeout=5000)
    except Exception:
        return _fail(page, tenant, "forgot-password link")
    page.wait_for_timeout(1500)

    # 3. Submit the reset request with the account email.
    frame, _ = _find_frame_locator(
        page, "[data-automation-id='resetPasswordButton']")
    if frame is None:
        return _fail(page, tenant, "reset request form")
    requested_at = int(time.time())
    try:
        frame.locator("input[data-automation-id='email']").last.fill(address)
        wd._click_workday_submit(frame, "resetPasswordButton")
    except Exception as exc:
        return _fail(page, tenant, "reset request submit", type(exc).__name__)
    page.wait_for_timeout(2500)

    # 4. Reset email (link, or the older security-code variant).
    found = fetch_reset_email(tenant, host, requested_at,
                              timeout_s=poll_timeout_s)
    if not found:
        return _fail(page, tenant, "reset email (not received)")
    if found.get("url"):
        try:
            page.goto(found["url"], wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(3000)
        except Exception as exc:
            return _fail(page, tenant, "open reset link", type(exc).__name__)
    else:
        code_frame, code_input = _find_frame_locator(
            page,
            "input[data-automation-id='verificationCode'], "
            "input[data-automation-id*='securityCode'], "
            "input[data-automation-id*='Code']",
        )
        if code_input is None:
            return _fail(page, tenant, "security code entry")
        try:
            code_input.fill(found["code"])
            wd._click_workday_submit(code_frame, "resetPasswordButton")
        except Exception as exc:
            return _fail(page, tenant, "security code submit",
                         type(exc).__name__)
        page.wait_for_timeout(3000)

    # 5. Set the new password. Store it BEFORE submitting so a crash after
    # Workday accepts the form can never lose the only working credential.
    frame, _ = _find_frame_locator(
        page, "[data-automation-id='resetPasswordButton']")
    if frame is None or not frame.locator(
            "input[data-automation-id='password']").count():
        return _fail(page, tenant, "new password form")
    password = generate_password()
    store_password(tenant, address, password)
    try:
        frame.locator("input[data-automation-id='password']").last.fill(password)
        verify = frame.locator("input[data-automation-id='verifyPassword']")
        if verify.count():
            verify.last.fill(password)
        wd._click_workday_submit(frame, "resetPasswordButton")
    except Exception as exc:
        return _fail(page, tenant, "new password submit", type(exc).__name__)
    page.wait_for_timeout(4000)
    error = wd._workday_auth_error(page)
    if error:
        return _fail(page, tenant, "new password submit", error)

    # 6. Confirm the reset landed: sign in with the stored credentials.
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except Exception as exc:
        return _fail(page, tenant, "sign-in confirmation", type(exc).__name__)
    wd._open_email_auth(page, create_account=False)
    wd.maybe_sign_in(page, tenant)
    page.wait_for_timeout(1500)
    if wd._workday_auth_gate_visible(page):
        return _fail(page, tenant, "sign-in confirmation",
                     wd._workday_auth_error(page))
    return True, ""


if __name__ == "__main__":
    print(recover_account(sys.argv[1], sys.argv[2]))
