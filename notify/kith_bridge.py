"""Send jobhunt messages to David's phone via the kith-bridge outbox.

kith-bridge (moscow/kith-bridge) is the always-on messaging daemon on this
Mac. Its send path is a queue: rows inserted into kith's Supabase
`assistant_outbox` table are drained every ~60s by the bridge and delivered
to David over its configured transport (iMessage AppleScript, or the
Telegram bot since 2026-08-01 — same phone either way). Inserting a row IS
the send; the bridge being down only delays delivery to its next drain.
App-side etiquette applies: delivery holds to 8am-9pm David-local and rows
pending >12h are auto-canceled — fine for a 6pm digest.

Reuses kith's env exactly like notify/kith_token.py: no new secrets stored.
"""
from __future__ import annotations

from notify import kith_token


def send_phone(body: str) -> None:
    """Queue one message for David's phone (kith's assistant_outbox).

    Raises on any failure (env missing, Supabase unreachable) — callers
    treat the phone channel as best-effort and must fail soft.
    """
    env = kith_token._kith_env()
    owner = env.get("OWNER_ID")
    if not owner:
        raise RuntimeError("OWNER_ID missing from kith .env.local")
    kith_token._rest(env, "POST", "/assistant_outbox",
                     {"owner_id": owner, "channel": "phone", "body": body})
