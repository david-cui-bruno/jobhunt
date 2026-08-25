from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from submission.attempts import ensure_submission_attempts, finish_attempt, start_attempt
from submission.lanes import LanePolicy, classify_url


def run_adapter(payload: dict) -> dict:
    import submit
    return submit._isolated_adapter(payload)


def _submit_helpers():
    import submit
    return submit


def _runtime_path(path: str) -> Path:
    return _submit_helpers()._runtime_path(path)


def _resume_quality_ready(resume_pdf: Path, posting_id: str) -> tuple[bool, str]:
    return _submit_helpers()._resume_quality_ready(resume_pdf, posting_id)


def _posting_dead(url: str) -> bool:
    return _submit_helpers()._posting_dead(url)


def _enforce_submission_safety(result: dict, attempted: bool) -> dict:
    return _submit_helpers()._enforce_submission_safety(result, attempted)


def _mark_outcome(*args, **kwargs) -> bool:
    return _submit_helpers()._mark_outcome(*args, **kwargs)


def _outcome(result: dict) -> str:
    return _submit_helpers()._outcome(result)


def _send_notice(subject: str, body: str) -> bool:
    return _submit_helpers()._send_notice(subject, body)


def _row_get(row: sqlite3.Row, key: str, default=None):
    return row[key] if key in row.keys() else default


def _status_for_result(outcome: str, result: dict, attempts: int) -> str:
    if result.get("submission_uncertain"):
        return "manual"
    if outcome == "retryable_failure" and attempts < 2:
        return "ready"
    return {
        "submitted": "submitted",
        "manual": "manual",
        "stale": "filtered_out",
        "retryable_failure": "failed",
        "failed": "failed",
    }.get(outcome, "failed")


def _finish_attempt_once(conn: sqlite3.Connection, *, attempt_id: str, result: dict, outcome: str, reason: str) -> None:
    finish_attempt(
        conn,
        attempt_id=attempt_id,
        outcome=outcome,
        reason_code=outcome,
        raw_reason=reason,
        click_attempted=bool(result.get("click_attempted")),
        confirmation_observed=bool(result.get("submitted") or outcome == "submitted"),
        artifact_refs=result.get("artifact_refs") or {},
        unanswered=result.get("unanswered") or [],
    )


def execute_claimed_posting(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    lane: LanePolicy,
    dry_run: bool,
    worker_id: str,
) -> dict:
    """Execute one already-claimed posting and persist its terminal or retry state."""
    ensure_submission_attempts(conn)
    posting_id = row["posting_id"]
    company = row["company"]
    title = row["title"]
    url = row["url"]
    pdf = _runtime_path(row["resume_pdf"])

    if dry_run:
        current = conn.execute("SELECT status FROM postings WHERE posting_id=?", (posting_id,)).fetchone()
        if current is None or current[0] != "ready":
            return {
                "company": company,
                "ats": "unknown",
                "outcome": "skipped",
                "reason": "dry-run row no longer ready",
            }

    quality_ok, quality_reason = _resume_quality_ready(pdf, posting_id)
    if not quality_ok:
        reason = f"resume quality gate: {quality_reason}"
        if not dry_run:
            _mark_outcome(
                conn,
                posting_id,
                "manual",
                "manual",
                reason,
                dry_run,
                expected_status="submitting",
            )
        return {"company": company, "ats": "unknown", "outcome": "manual", "reason": reason, "pre_attempt": True}

    if _posting_dead(url):
        _mark_outcome(
            conn,
            posting_id,
            "filtered_out",
            "stale",
            "liveness check marked posting stale",
            dry_run,
            expected_status=None if dry_run else "submitting",
        )
        return {"company": company, "ats": "unknown", "outcome": "stale", "reason": "liveness check marked posting stale", "pre_attempt": True}

    attempt_id = uuid.uuid4().hex
    ats, _detected_lane = classify_url(url)
    slug = f"{company.replace(' ', '_')[:40]}_{int(time.time())}"
    res: dict = {}
    outcome = "failed"
    reason = ""
    launched = False
    try:
        if not dry_run:
            start_attempt(
                conn,
                attempt_id=attempt_id,
                posting_id=posting_id,
                ats=ats,
                lane=lane.name,
                worker_id=worker_id,
                browser_mode="isolated_subprocess",
                policy_revision="2026-08-22-local-ats-dispatcher",
            )
        launched = True
        try:
            res = run_adapter({
                "url": url,
                "resume_pdf": str(pdf),
                "slug": slug,
                "posting_id": posting_id,
                "company": company,
                "title": title,
                "locations": _row_get(row, "locations", "") or "",
                "tracker_db": str(conn.execute("PRAGMA database_list").fetchone()[2]),
                "dry_run": dry_run,
            })
        except Exception as exc:
            res = {
                "outcome": "manual",
                "ok": False,
                "submitted": False,
                "retryable": False,
                "click_attempted": True,
                "submission_uncertain": True,
                "reason": f"adapter launch failed: {type(exc).__name__}: {exc}; verify possible prior submission",
            }
        res = _enforce_submission_safety(res, bool(res.get("click_attempted")))
        outcome = _outcome(res)
        reason = str(res.get("reason", ""))

        attempts = (_row_get(row, "attempt_count", 0) or 0)
        status_for_outcome = _status_for_result(outcome, res, attempts)
        changed = _mark_outcome(
            conn,
            posting_id,
            status_for_outcome,
            outcome,
            reason,
            dry_run,
            expected_status=None if dry_run else "submitting",
            commit=not (outcome == "submitted" and not dry_run),
        )
        if outcome == "submitted" and not dry_run:
            if not changed:
                conn.rollback()
                outcome = "manual"
                reason = "submission claim lost; verify"
                res = dict(res)
                res["outcome"] = "manual"
                res["submitted"] = False
                res["submission_uncertain"] = False
                return {
                    "company": company,
                    "ats": res.get("detected_ats", "unknown"),
                    "outcome": outcome,
                    "reason": reason,
                    "attempt_id": attempt_id,
                }
            detected_ats = str(res.get("detected_ats", "unknown"))
            notes = str(_row_get(row, "application_notes", "") or "")
            try:
                conn.execute(
                    "INSERT INTO applications VALUES (?,?,?,?,?,?)",
                    (posting_id, str(pdf), detected_ats, int(time.time()), reason, notes),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                outcome = "manual"
                reason = "already present in applications ledger; not resubmitted"
                res = dict(res)
                res["submitted"] = False
                res["outcome"] = outcome
                res["reason"] = reason
                conn.execute(
                    "UPDATE postings SET status='submitted', outcome='submitted', "
                    "last_error='already present in applications ledger' "
                    "WHERE posting_id=? AND status='submitting'",
                    (posting_id,),
                )
                conn.commit()
                return {
                    "company": company,
                    "ats": detected_ats,
                    "outcome": outcome,
                    "reason": reason,
                    "attempt_id": attempt_id,
                }
            conn.commit()
        elif res.get("submission_uncertain") and not dry_run:
            _send_notice(
                f"[jobhunt] verify possible submission: {company}",
                f"{company} — {title}\n{url}\n\n"
                f"The adapter attempted Submit but could not verify the result: {reason}\n"
                "This posting was quarantined and will not be retried automatically.",
            )
        elif outcome == "manual" and res.get("unanswered") and not dry_run:
            _send_notice(
                f"[jobhunt] manual input needed: {company}",
                f"{company} — {title}\n{url}\n\n"
                f"Auto-fill couldn't answer: {res['unanswered']}\n"
                "Reply with answers and I'll retry, or apply manually.",
            )
        output = {"company": company, "ats": res.get("detected_ats", ats), "outcome": outcome, "reason": reason}
        if launched and not dry_run:
            output["attempt_id"] = attempt_id
        for key in ("submission_uncertain", "click_attempted", "definitive_rejection", "unanswered", "artifact_refs"):
            if key in res:
                output[key] = res[key]
        return output
    finally:
        if launched and not dry_run:
            _finish_attempt_once(conn, attempt_id=attempt_id, result=res, outcome=outcome, reason=reason)
