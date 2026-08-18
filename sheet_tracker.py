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
        ],
    })
    sid = created["spreadsheetId"]
    STATE.write_text(json.dumps({"spreadsheet_id": sid, "url": created.get("spreadsheetUrl")}))
    return sid


def _sheet_ids(sid: str) -> dict[str, int]:
    meta = _api("GET", f"/{sid}?fields=sheets.properties")
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


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
        if ev:
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
    ids = _sheet_ids(sid)
    apps, pipeline, s = _collect()
    now = datetime.datetime.now(ET).strftime("%b %-d, %-I:%M %p ET")

    resp_rate = f"{100 * s['responded'] // s['total']}%" if s["total"] else "0%"
    dashboard = [
        ["DAVID'S APPLICATION TRACKER", "", "", f"last sync: {now}"],
        [],
        ["applications", "", "outcomes", ""],
        ["this week", s["week"], "OAs received", s["oa"]],
        ["total submitted", s["total"], "interviews", s["interview"]],
        ["response rate", resp_rate, "offers", s["offer"]],
        ["", "", "rejections", s["rejected"]],
        [],
        ["pipeline right now", "", "", ""],
        ["about to submit", sum(1 for p in pipeline if p[2] in ("ready", "submitting")), "in tailoring", sum(1 for p in pipeline if p[2] in ("queued", "tailoring", "tailored", "sprinting"))],
    ]

    app_rows = [["Company", "Role", "Applied", "Status", "Source", "ATS", "Location", "Last event", "Link"]] + apps
    pipe_rows = [["Company", "Role", "Stage", "Source", "Link"]] + pipeline

    # Full clear + rewrite (one-way view; cheap at this scale).
    _api("POST", f"/{sid}/values:batchClear", {"ranges": ["Dashboard!A1:Z100", "Applications!A1:Z5000", "Pipeline!A1:Z5000"]})
    _api("POST", f"/{sid}/values:batchUpdate", {
        "valueInputOption": "RAW",
        "data": [
            {"range": "Dashboard!A1", "values": dashboard},
            {"range": "Applications!A1", "values": app_rows},
            {"range": "Pipeline!A1", "values": pipe_rows},
        ],
    })

    # Formatting: header bold, badge conditional colors, sensible widths.
    fmt: list[dict] = []
    for tab in ("Dashboard", "Applications", "Pipeline"):
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
    _api("POST", f"/{sid}:batchUpdate", {"requests": fmt})

    url = json.loads(STATE.read_text()).get("url") or f"https://docs.google.com/spreadsheets/d/{sid}"
    return url


if __name__ == "__main__":
    print(sync())
