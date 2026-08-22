#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from submission.ashby_policy import INTERVAL_MINUTES, can_attempt, enable_canary, load_state, set_enabled
from submission.database import DB, connect_tracker
from submission.dispatcher import eligible_ashby_posting
from submission.executor import _resume_quality_ready, _runtime_path
from submission.lanes import ASHBY, classify_url


def _ready_depth(conn) -> int:
    count = 0
    for row in conn.execute("SELECT url FROM postings WHERE status='ready'").fetchall():
        _ats, lane = classify_url(row["url"])
        if lane.name == ASHBY.name:
            count += 1
    return count


def _status(conn) -> dict:
    state = load_state(conn)
    return {
        "ats": "ashby",
        "enabled": state.enabled,
        "tier": state.tier,
        "interval_minutes": INTERVAL_MINUTES[state.tier],
        "consecutive_confirmed": state.consecutive_confirmed,
        "next_attempt_at": state.next_attempt_at,
        "blocked_until": state.blocked_until,
        "last_outcome": state.last_outcome,
        "ready_depth": _ready_depth(conn),
        "eligible_posting_id": eligible_ashby_posting(conn, now=int(time.time())),
    }


def _preview(conn) -> dict:
    state = load_state(conn)
    posting_id = eligible_ashby_posting(conn, now=int(time.time()))
    if posting_id is None:
        code = "no_eligible_candidate" if can_attempt(state, now=int(time.time())) else "policy_not_due"
        return {"ats": "ashby", "candidate": None, "reason": {"code": code}}
    row = conn.execute(
        "SELECT p.posting_id,p.company,p.title,p.url,e.resume_pdf FROM postings p JOIN emails e USING(posting_id) WHERE p.posting_id=?",
        (posting_id,),
    ).fetchone()
    ok, reason = _resume_quality_ready(_runtime_path(row["resume_pdf"]), posting_id)
    prior = conn.execute("SELECT COUNT(*) FROM submission_attempts WHERE posting_id=? AND finished_at IS NOT NULL", (posting_id,)).fetchone()[0]
    return {
        "ats": "ashby",
        "posting_id": posting_id,
        "company": row["company"],
        "title": row["title"],
        "canonical_url": row["url"],
        "completed_prior_attempt_count": prior,
        "quality": {"ready": ok, "reason": reason},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("command", choices=("status", "preview", "enable-canary", "pause"))
    parser.add_argument("lane")
    args = parser.parse_args(argv)
    if args.lane != "ashby":
        print(json.dumps({"error": "unsupported lane"}), file=sys.stderr)
        return 2
    conn = connect_tracker(args.db)
    try:
        if args.command == "status":
            output = _status(conn)
        elif args.command == "preview":
            output = _preview(conn)
        elif args.command == "enable-canary":
            state = enable_canary(conn, now=int(time.time()))
            output = {"ats": "ashby", "enabled": state.enabled, "tier": state.tier, "next_attempt_at": state.next_attempt_at}
        else:
            state = set_enabled(conn, False, now=int(time.time()))
            output = {"ats": "ashby", "enabled": state.enabled}
        print(json.dumps(output, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
