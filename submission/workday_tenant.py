from __future__ import annotations

import re
import urllib.parse


def workday_tenant_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if not host.endswith(".myworkdayjobs.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    site = parts[0].lower() if parts else ""
    return f"{host}/{site}"
