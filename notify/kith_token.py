"""Fallback Google access token from kith's integration (same Gmail account).

jobhunt's own OAuth token (secrets/gmail_token.json) dies whenever Google
revokes it (observed invalid_grant 2026-08-17). Kith keeps a healthy
refresh-token for the SAME gmail account in its Supabase `integrations` row,
with gmail.readonly + gmail.send scopes — enough for everything jobhunt's
mailer does. This module mints a fresh access token from that row so email
keeps flowing with zero re-auth clicking.

Reads kith's env from the moscow checkout; no new secrets are stored here.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

KITH_ENV = Path("/Users/davidcui824/conductor/workspaces/crm/moscow/.env.local")
_cache: dict = {"token": None, "exp": 0.0}


def _kith_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in KITH_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _rest(env: dict, method: str, path: str, body: dict | None = None) -> list | dict:
    req = urllib.request.Request(
        f"{env['NEXT_PUBLIC_SUPABASE_URL']}/rest/v1{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "apikey": env["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {env['SUPABASE_SERVICE_ROLE_KEY']}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def access_token() -> str:
    """A valid access token for the kith Google connection (cached in-process)."""
    if _cache["token"] and _cache["exp"] > time.time() + 120:
        return _cache["token"]
    env = _kith_env()
    rows = _rest(env, "GET", "/integrations?provider=eq.google&select=id,access_token,refresh_token,token_expiry")
    if not rows:
        raise RuntimeError("kith google integration row not found")
    row = rows[0]
    exp = 0.0
    if row.get("token_expiry"):
        try:
            exp = time.mktime(time.strptime(row["token_expiry"][:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            exp = 0.0
    if exp > time.time() + 120:
        _cache.update(token=row["access_token"], exp=exp)
        return row["access_token"]
    # refresh
    data = urllib.parse.urlencode({
        "refresh_token": row["refresh_token"],
        "client_id": env["GOOGLE_CLIENT_ID"],
        "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    expires_at = time.time() + int(tok.get("expires_in", 3600))
    # write back so kith + other callers share the fresh token (UTC ISO, matching kith)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(expires_at)) + "+00:00"
    _rest(env, "PATCH", f"/integrations?id=eq.{row['id']}",
          {"access_token": tok["access_token"], "token_expiry": iso})
    _cache.update(token=tok["access_token"], exp=expires_at)
    return tok["access_token"]
