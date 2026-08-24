"""Visual application tracker: one-way sync from tracker.db to a Google Sheet.

Three tabs, refreshed by the inbox timer (~30 min):
  Dashboard    - stats header: apps this week/total, response/OA/interview
                 rates, pipeline counts, last sync stamp
  Applications - one row per SUBMITTED application, newest first, with a
                 color-coded status badge derived from inbox_events
  Pipeline     - live queue (queued/tailored/ready), what's about to go out

One-way by design (David 2026-08-18): the sheet is a VIEW; edits there are
never read back. The whole grid is rewritten every sync so it can't drift.

Status derivation (per company, newest event wins):
  offer > interview > oa > rejected > ghosted(30d silent) > submitted

Needs the spreadsheets scope on secrets/gmail_token.json (deploy/reauth_gmail.py).
The sheet id is cached in out/sheet_tracker.json after first creation.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from notify import mailer

ROOT = Path(__file__).resolve().parent
DB = ROOT / "out" / "tracker.db"
STATE = ROOT / "out" / "sheet_tracker.json"
ET = ZoneInfo("America/New_York")
API = "https://sheets.googleapis.com/v4/spreadsheets"

TITLE = "David — Job Applications"

BADGE = {
    "offer": "🟢 OFFER",
    "interview": "🟣 Interview",
    "oa": "🟠 OA received",
    "rejected": "🔴 Rejected",
    "ghosted": "⚫ Ghosted",
    "submitted": "🔵 Submitted",
}
# Badge -> background/text color for conditional formatting.
BADGE_COLORS = {
    "🟢 OFFER": {"bg": (0.85, 0.94, 0.83), "fg": (0.13, 0.42, 0.15)},
    "🟣 Interview": {"bg": (0.90, 0.85, 0.96), "fg": (0.35, 0.16, 0.55)},
    "🟠 OA received": {"bg": (0.99, 0.91, 0.79), "fg": (0.65, 0.36, 0.02)},
    "🔴 Rejected": {"bg": (0.97, 0.84, 0.84), "fg": (0.66, 0.11, 0.11)},
    "⚫ Ghosted": {"bg": (0.93, 0.93, 0.93), "fg": (0.35, 0.35, 0.35)},
    "🔵 Submitted": {"bg": (0.84, 0.90, 0.97), "fg": (0.08, 0.32, 0.60)},
}

RESPONSE_EVENTS = {"offer", "interview_invite", "oa_invite", "recruiter_reply", "rejection"}


def _is_response_event(category: str) -> bool:
    return category in RESPONSE_EVENTS


def _api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {mailer._bearer()}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _spreadsheet_id() -> str:
    if STATE.exists():
        try:
            sid = json.loads(STATE.read_text()).get("spreadsheet_id")
            if sid:
                return sid
        except json.JSONDecodeError:
            pass
    created = _api("POST", "", {
        "properties": {"title": TITLE},
        "sheets": [
            {"properties": {"title": "Dashboard", "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"title": "Applications", "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"title": "Pipeline", "gridProperties": {"frozenRowCount": 1}}},
            {"properties": {"title": "Manual Actions", "gridProperties": {"frozenRowCount": 1}}},
        ],
    })
    sid = created["spreadsheetId"]
    STATE.write_text(json.dumps({"spreadsheet_id": sid, "url": created.get("spreadsheetUrl")}))
    return sid


def _sheet_ids(sid: str) -> dict[str, int]:
    meta = _api("GET", f"/{sid}?fields=sheets.properties")
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def _ensure_sheet(sid: str, title: str) -> int:
    ids = _sheet_ids(sid)
    if title in ids:
        return ids[title]
    created = _api("POST", f"/{sid}:batchUpdate", {"requests": [{"addSheet": {"properties": {"title": title, "gridProperties": {"frozenRowCount": 1}}}}]})
    return created["replies"][0]["addSheet"]["properties"]["sheetId"]


MANUAL_ACTION_HEADERS = [
    "Company", "Role", "ATS", "Action", "URL", "Age", "Attempt state",
    "Prepared resume", "Prepared screenshot", "Latest reason",
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _safe_reason(reason: str) -> str:
    text = re.sub(r"/Users/\S+", "[local path omitted]", reason or "")
    text = re.sub(r"raw answer\s*[:=]?\s*\S+", "[answer omitted]", text, flags=re.IGNORECASE)
    return text[:300]


def _manual_action(reason: str, click_attempted: int | None, confirmed: int | None) -> str | None:
    lower = (reason or "").lower()
    if click_attempted and not confirmed:
        return "Verify before retrying"
    if "captcha" in lower or "datadome" in lower:
        return "Complete CAPTCHA"
    if "manual" in lower and ("completion" in lower or "field" in lower or "location" in lower or "trusted-browser" in lower):
        return "Finish manually"
    return None


def _collect_manual_actions(conn: sqlite3.Connection) -> list[list]:
    has_attempts = _table_exists(conn, "submission_attempts")
    email_cols = _columns(conn, "emails")
    resume_col = "resume_pdf" if "resume_pdf" in email_cols else "resume_path" if "resume_path" in email_cols else None
    resume_select = f"EXISTS (SELECT 1 FROM emails e WHERE e.posting_id = p.posting_id AND COALESCE(e.{resume_col}, '') != '')" if resume_col else "0"
    latest_attempt = """
        LEFT JOIN (
            SELECT sa.* FROM submission_attempts sa
            JOIN (
                SELECT posting_id, MAX(started_at) AS started_at
                FROM submission_attempts GROUP BY posting_id
            ) latest USING (posting_id, started_at)
        ) sa ON sa.posting_id = p.posting_id
    """ if has_attempts else ""
    attempt_fields = "sa.outcome, sa.reason_code, sa.raw_reason, sa.click_attempted, sa.confirmation_observed, sa.artifact_refs_json," if has_attempts else "NULL AS outcome, NULL AS reason_code, NULL AS raw_reason, NULL AS click_attempted, NULL AS confirmation_observed, NULL AS artifact_refs_json,"
    rows = conn.execute(f"""
        SELECT p.company, p.title, p.ats, p.url, p.last_error, p.first_seen, p.last_attempt_at,
               {resume_select} AS prepared_resume,
               {attempt_fields}
               p.posting_id
        FROM postings p
        {latest_attempt}
        WHERE p.status = 'manual'
        ORDER BY COALESCE(p.last_attempt_at, p.first_seen, 0) DESC, p.rowid DESC
    """).fetchall()

    now = int(time.time())
    output = [MANUAL_ACTION_HEADERS[:]]
    for row in rows:
        reason = row["last_error"] or row["raw_reason"] or row["reason_code"] or ""
        action_reason = " ".join(filter(None, [row["reason_code"], row["last_error"], row["raw_reason"]]))
        action = _manual_action(action_reason, row["click_attempted"], row["confirmation_observed"])
        if not action:
            continue
        artifacts = json.loads(row["artifact_refs_json"] or "{}") if row["artifact_refs_json"] else {}
        prepared_screenshot = any("screenshot" in key and value for key, value in artifacts.items())
        age_source = row["last_attempt_at"] or row["first_seen"] or 0
        age_days = max(0, (now - int(age_source)) // 86400) if age_source else ""
        output.append([
            row["company"] or "?",
            row["title"] or "?",
            row["ats"] or "",
            action,
            row["url"] or "",
            f"{age_days}d" if age_days != "" else "",
            row["outcome"] or row["reason_code"] or "manual",
            bool(row["prepared_resume"]),
            bool(prepared_screenshot),
            _safe_reason(reason),
        ])
    return output


def _status_for(company: str, submitted_at: int, events: list[tuple[str, int]]) -> tuple[str, str]:
    """(badge, last_event_text) from this company's inbox events."""
    rank = {"offer": 5, "interview_invite": 4, "oa_invite": 3, "rejection": 2}
    best, best_ts, last_txt = None, 0, ""
    for cat, ts in events:
        if ts and ts >= submitted_at - 86400:  # events at/after the application
            if rank.get(cat, 0) > rank.get(best or "", 0):
                best, best_ts = cat, ts
    if events:
        cat, ts = max(events, key=lambda e: e[1])
        when = datetime.datetime.fromtimestamp(ts, ET).strftime("%b %-d")
        nice = {"oa_invite": "OA", "interview_invite": "interview", "recruiter_reply": "recruiter reply",
                "rejection": "rejection", "offer": "offer", "confirmation": "confirmation"}.get(cat, cat)
        last_txt = f"{nice} {when}"
    if best == "offer":
        return BADGE["offer"], last_txt
    if best == "interview_invite":
        return BADGE["interview"], last_txt
    if best == "oa_invite":
        return BADGE["oa"], last_txt
    if best == "rejection":
        return BADGE["rejected"], last_txt
    if time.time() - submitted_at > 30 * 86400:
        return BADGE["ghosted"], last_txt
    return BADGE["submitted"], last_txt


def _collect() -> tuple[list[list], list[list], dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # inbox events grouped by normalized company
    events: dict[str, list[tuple[str, int]]] = {}
    try:
        for r in conn.execute("SELECT company, category, ts FROM inbox_events WHERE company != ''"):
            events.setdefault(r["company"].strip().lower(), []).append((r["category"], r["ts"] or 0))
    except sqlite3.OperationalError:
        pass  # inbox_events may not exist on a fresh db

    apps: list[list] = []
    subs = conn.execute(
        "SELECT a.posting_id, a.ats, a.submitted_at, p.company, p.title, p.locations, p.url, p.source "
        "FROM applications a JOIN postings p USING(posting_id) ORDER BY a.submitted_at DESC").fetchall()
    stats = {"total": len(subs), "week": 0, "oa": 0, "interview": 0, "offer": 0, "rejected": 0, "responded": 0}
    week_ago = time.time() - 7 * 86400
    for r in subs:
        ts = r["submitted_at"] or 0
        if ts > week_ago:
            stats["week"] += 1
        ev = events.get((r["company"] or "").strip().lower(), [])
        badge, last = _status_for(r["company"] or "", ts, ev)
        if badge == BADGE["oa"]:
            stats["oa"] += 1
        elif badge == BADGE["interview"]:
            stats["interview"] += 1
        elif badge == BADGE["offer"]:
            stats["offer"] += 1
        elif badge == BADGE["rejected"]:
            stats["rejected"] += 1
        if any(_is_response_event(category) for category, _ in ev):
            stats["responded"] += 1
        apps.append([
            r["company"] or "?", r["title"] or "?",
            datetime.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d") if ts else "",
            badge, r["source"] or "", r["ats"] or "", (r["locations"] or "")[:40],
            last, r["url"] or "",
        ])

    pipe = conn.execute(
        "SELECT company, title, status, source, url FROM postings "
        "WHERE status IN ('queued','tailoring','tailored','sprinting','ready','submitting') "
        "ORDER BY CASE status WHEN 'submitting' THEN 0 WHEN 'ready' THEN 1 WHEN 'tailored' THEN 2 ELSE 3 END, rowid DESC").fetchall()
    pipeline = [[r["company"] or "?", r["title"] or "?", r["status"], r["source"] or "", r["url"] or ""] for r in pipe]
    conn.close()
    return apps, pipeline, stats


def sync() -> str:
    sid = _spreadsheet_id()
    manual_sheet_id = _ensure_sheet(sid, "Manual Actions")
    ids = _sheet_ids(sid)
    apps, pipeline, s = _collect()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    manual_actions = _collect_manual_actions(conn)
    conn.close()
    now = datetime.datetime.now(ET).strftime("%b %-d, %-I:%M %p ET")

    resp_rate = f"{100 * s['responded'] // s['total']}%" if s["total"] else "0%"
    ready_ct = sum(1 for p in pipeline if p[2] in ("ready", "submitting"))
    cooking_ct = sum(1 for p in pipeline if p[2] in ("queued", "tailoring", "tailored", "sprinting"))
    dashboard = [
        ["david's job hunt", "", "", f"updated {now.lower()}"],
        [],
        [f"{s['week']} sent this week", "", f"{s['total']} sent all time", ""],
        [f"{resp_rate} heard back", "", f"{ready_ct} ready to go, {cooking_ct} cooking", ""],
        [],
        ["how it's going", "", "", ""],
        [f"🟠 {s['oa']} OAs", f"🟣 {s['interview']} interviews", f"🟢 {s['offer']} offers", f"🔴 {s['rejected']} nos"],
        [],
        ["the bots apply, statuses update themselves. don't edit — this sheet rewrites itself.", "", "", ""],
    ]

    app_rows = [["Company", "Role", "Applied", "Status", "Source", "ATS", "Location", "Last event", "Link"]] + apps
    pipe_rows = [["Company", "Role", "Stage", "Source", "Link"]] + pipeline

    # Full clear + rewrite (one-way view; cheap at this scale).
    _api("POST", f"/{sid}/values:batchClear", {"ranges": ["Dashboard!A1:Z100", "Applications!A1:Z5000", "Pipeline!A1:Z5000", "Manual Actions!A:J"]})
    _api("POST", f"/{sid}/values:batchUpdate", {
        "valueInputOption": "RAW",
        "data": [
            {"range": "Dashboard!A1", "values": dashboard},
            {"range": "Applications!A1", "values": app_rows},
            {"range": "Pipeline!A1", "values": pipe_rows},
            {"range": "Manual Actions!A1", "values": manual_actions},
        ],
    })

    # Formatting: header bold, badge conditional colors, sensible widths.
    fmt: list[dict] = []
    ids["Manual Actions"] = manual_sheet_id
    for tab in ("Dashboard", "Applications", "Pipeline", "Manual Actions"):
        fmt.append({"repeatCell": {
            "range": {"sheetId": ids[tab], "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}})
    # badge colors on Applications!D
    del_existing = {"deleteConditionalFormatRule": {"sheetId": ids["Applications"], "index": 0}}
    for _ in range(6):  # drop stale rules (ignore errors server-side by try below)
        try:
            _api("POST", f"/{sid}:batchUpdate", {"requests": [del_existing]})
        except Exception:
            break
    for badge, c in BADGE_COLORS.items():
        fmt.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": ids["Applications"], "startRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 4}],
            "booleanRule": {
                "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": badge.split(" ", 1)[1]}]},
                "format": {
                    "backgroundColor": {"red": c["bg"][0], "green": c["bg"][1], "blue": c["bg"][2]},
                    "textFormat": {"bold": True, "foregroundColor": {"red": c["fg"][0], "green": c["fg"][1], "blue": c["fg"][2]}},
                }}}, "index": 0}})
    fmt.append({"updateDimensionProperties": {
        "range": {"sheetId": ids["Applications"], "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
        "properties": {"pixelSize": 190}, "fields": "pixelSize"}})
    fmt.extend([
        {"setBasicFilter": {"filter": {"range": {"sheetId": manual_sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 10}}}},
        {"repeatCell": {
            "range": {"sheetId": manual_sheet_id, "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.89, "green": 0.95, "blue": 1.0}, "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(backgroundColor,wrapStrategy)"}},
        {"repeatCell": {
            "range": {"sheetId": manual_sheet_id, "startColumnIndex": 6, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80}, "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(backgroundColor,wrapStrategy)"}},
        {"repeatCell": {
            "range": {"sheetId": manual_sheet_id, "startColumnIndex": 0, "endColumnIndex": 10},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy"}},
    ])
    _api("POST", f"/{sid}:batchUpdate", {"requests": fmt})

    url = json.loads(STATE.read_text()).get("url") or f"https://docs.google.com/spreadsheets/d/{sid}"
    return url


if __name__ == "__main__":
    print(sync())
