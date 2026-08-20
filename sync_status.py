"""Push a compact jobhunt status blob into kith's agent_memory.

David 2026-08-19: "I'd rather have jobhunt be sent to my telegram via kith."
The kith agent (Telegram/iMessage) answers questions from its Supabase; it
cannot see this Mac's tracker.db. job_queue_snapshot already mirrors active
postings, but the agent also needs the DIGEST-level picture: pipeline counts,
today's submissions, inbox action items, and what's stuck waiting on David.

One row: agent_memory key 'jobhunt:status' (owner-scoped, upsert). The kith
jobhunt_status tool reads it. Runs from inbox.py's 30-min timer right after
sheet_tracker; failures are one log line, never fatal.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")


def build_status(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM postings GROUP BY status")}
    day_start = int(datetime.datetime.now(ET).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    submitted_today = [
        {"company": r["company"], "title": r["title"][:80]}
        for r in conn.execute(
            "SELECT p.company, p.title FROM applications a JOIN postings p USING(posting_id) "
            "WHERE a.submitted_at >= ? ORDER BY a.submitted_at DESC", (day_start,))
    ]
    week_ago = int(time.time()) - 7 * 86400
    action = [
        {"kind": r["category"], "company": r["company"], "role": (r["role"] or "")[:60],
         "deadline": r["deadline"], "summary": (r["summary"] or "")[:140]}
        for r in conn.execute(
            "SELECT category, company, role, deadline, summary FROM inbox_events "
            "WHERE category IN ('oa_invite','interview_invite','recruiter_reply','offer') "
            "AND ts >= ? ORDER BY ts DESC LIMIT 12", (week_ago,))
    ]
    stuck = [
        {"company": r["company"], "title": r["title"][:60], "ask": (r["last_error"] or "")[:160],
         "url": r["url"]}
        for r in conn.execute(
            "SELECT company, title, last_error, url FROM postings WHERE status='manual' "
            "AND last_error LIKE 'needs answers%' ORDER BY last_attempt_at DESC LIMIT 15")
    ]
    apply_by_hand = [
        {"company": r["company"], "title": r["title"][:60], "url": r["url"]}
        for r in conn.execute(
            "SELECT company, title, url FROM postings WHERE status='manual' "
            "AND (last_error LIKE 'unsupported ATS%' OR last_error LIKE '%CAPTCHA%') "
            "ORDER BY last_attempt_at DESC LIMIT 12")
    ]
    return {
        "as_of": datetime.datetime.now(ET).isoformat(timespec="minutes"),
        "pipeline": {
            "queued": counts.get("queued", 0) + counts.get("tailoring", 0) + counts.get("sprinting", 0),
            "ready": counts.get("ready", 0) + counts.get("submitting", 0),
            "manual": counts.get("manual", 0),
            "submitted_total": counts.get("submitted", 0),
            "failed": counts.get("failed", 0),
        },
        "submitted_today": submitted_today[:20],
        "submitted_today_count": len(submitted_today),
        "action_items": action,
        "stuck_on_questions": stuck,
        "apply_by_hand": apply_by_hand,
        "reply_commands": 'reply "skip <company>" or "<company>: <answer>" to unstick; mention jobhunt',
    }


def sync() -> dict:
    from sync_queue import load_env_file, required_env, SupabaseRest  # reuse env plumbing
    load_env_file(Path.home() / "conductor/workspaces/crm/moscow/.env.local")
    base_url = required_env("KITH_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    service_key = required_env("KITH_SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE_KEY")
    owner_id = required_env("KITH_OWNER_ID", "OWNER_ID")

    conn = sqlite3.connect(DB)
    status = build_status(conn)
    conn.close()

    client = SupabaseRest(base_url, service_key)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    endpoint = client.endpoint.replace("/job_queue_snapshot", "/agent_memory")
    client.request(
        "POST",
        endpoint + "?on_conflict=owner_id,key",
        [{"owner_id": owner_id, "key": "jobhunt:status",
          "value": status, "updated_at": now}],
        prefer="resolution=merge-duplicates",
    )
    return {"synced": True, "stuck": len(status["stuck_on_questions"]),
            "action": len(status["action_items"])}


if __name__ == "__main__":
    print(json.dumps(sync()))
