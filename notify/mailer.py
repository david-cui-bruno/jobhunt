"""Gmail send/read helpers using the saved OAuth token (secrets/gmail_token.json)."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

ROOT = Path(__file__).resolve().parent.parent
TOKEN = ROOT / "secrets" / "gmail_token.json"
ME = "davidcui824@gmail.com"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _creds() -> Credentials:
    c = Credentials.from_authorized_user_file(str(TOKEN))
    if c.expired or not c.valid:
        c.refresh(Request())
        TOKEN.write_text(c.to_json())
    return c


def _call(path: str, data: dict | None = None, method: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {_creds().token}",
                 "Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send(subject: str, body: str, attachments: list[Path] = (),
         thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
    # Self-notification emails (summaries, digests, FYI/error alerts) are muted
    # by default: David asked to stop receiving them (2026-08-17). They are
    # appended to out/notices.log instead. Set JOBHUNT_EMAIL_NOTICES=1 to
    # re-enable actual email delivery. Real applications to companies do not
    # go through this function.
    if os.environ.get("JOBHUNT_EMAIL_NOTICES") != "1":
        try:
            log = ROOT / "out" / "notices.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            att = f" attachments={[p.name for p in attachments]}" if attachments else ""
            with log.open("a") as f:
                f.write(f"{stamp} MUTED {subject}{att}\n{body}\n---\n")
        except Exception:
            pass
        return {}
    msg = EmailMessage()
    msg["To"] = ME
    msg["From"] = ME
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for p in attachments:
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return _call("/messages/send", payload)


def get_thread(thread_id: str) -> dict:
    return _call(f"/threads/{thread_id}?format=full")


def extract_plain(message: dict) -> str:
    """Get text/plain body of a gmail message, minus quoted reply lines."""
    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            t = walk(sub)
            if t:
                return t
        return ""
    text = walk(message.get("payload", {}))
    lines = [l for l in text.splitlines() if not l.strip().startswith(">")]
    # drop the "On ... wrote:" attribution line
    lines = [l for l in lines if not (l.strip().startswith("On ") and l.strip().endswith("wrote:"))]
    return "\n".join(lines).strip()


def headers_of(message: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
