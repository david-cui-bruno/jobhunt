"""Submitter: takes 'ready' postings and submits via the matching ATS adapter.

Pacing is bounded and configurable, but optimized for speed-to-apply. Unknown ATS
or adapter failure -> posting marked 'manual', summarized in the nightly email.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from submission.attempts import ensure_submission_attempts
from submission.database import connect_tracker
from submission.identity import claim_submission, posting_already_applied

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "apply"), str(ROOT / "notify")]

DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")
SUBMISSIONS_PER_RUN = int(os.environ.get("JOBHUNT_SUBMISSIONS_PER_RUN", "8"))
PACING_MIN_SECONDS = float(os.environ.get("JOBHUNT_PACING_MIN_SECONDS", "15"))
PACING_MAX_SECONDS = float(os.environ.get("JOBHUNT_PACING_MAX_SECONDS", "45"))
POSTING_TIMEOUT_SECONDS = int(os.environ.get("JOBHUNT_POSTING_TIMEOUT_SECONDS", "300"))
# Whole-run budget (2026-08-18): each posting is individually bounded at 5min,
# but a streak of slow-failing rows once stretched a run to 77 minutes, past
# the 65-min timer. Stop picking NEW rows once the budget is spent; the row in
# flight finishes under its own posting timeout.
RUN_BUDGET_SECONDS = int(os.environ.get("JOBHUNT_RUN_BUDGET_SECONDS", "2400"))
PLAYWRIGHT_TIMEOUT_MS = int(os.environ.get("JOBHUNT_PLAYWRIGHT_TIMEOUT_MS", "30000"))

if SUBMISSIONS_PER_RUN < 1:
    raise ValueError("JOBHUNT_SUBMISSIONS_PER_RUN must be at least 1")
if PACING_MIN_SECONDS < 0 or PACING_MAX_SECONDS < PACING_MIN_SECONDS:
    raise ValueError("invalid JOBHUNT pacing interval")


GAME_APPS = ("league of legends", "leagueclient", "riot client", "valorant", "steam_osx",
             "cs2", "dota 2", "minecraft")


def _dry_run_requested(argv: list[str]) -> bool:
    """Accept both documented spellings so a safety check can never run live."""
    return "--dry" in argv or "--dry-run" in argv


def _runtime_path(stored_path: str, root: Path = ROOT) -> Path:
    """Relocate a project file whose DB path came from another machine."""
    path = Path(stored_path)
    if not path.is_absolute():
        return root / path
    if path.exists():
        return path
    for marker in ("out", "resume"):
        if marker in path.parts:
            candidate = root.joinpath(*path.parts[path.parts.index(marker):])
            if candidate.exists():
                return candidate
    return path


def _user_is_gaming() -> bool:
    """True if a known game is running or the frontmost app is fullscreen."""
    import subprocess
    try:
        ps = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True, timeout=5).stdout.lower()
        if any(g in ps for g in GAME_APPS):
            return True
    except Exception:
        pass
    return False


DEAD_MARKERS = ("job not found", "no longer available", "job you requested was not found",
                "position has been filled", "posting is closed", "job posting is no longer",
                "job has expired", "this job has expired")


def _posting_dead(url: str) -> bool:
    """Cheap liveness check before spending a browser session.
    Ashby: authoritative board API (SPA hides deadness from raw HTTP).
    Others: body-text marker sniff."""
    try:
        from apply.jd import canonical_application_url
        url = canonical_application_url(url)
    except Exception:
        pass
    import json as _json
    import re as _re
    import urllib.error as _ue
    import urllib.request as _ur
    m = _re.search(r"ashbyhq\.com/([^/?]+)/([0-9a-f-]{36})", url)
    if m:
        try:
            req = _ur.Request(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}",
                              headers={"User-Agent": "Mozilla/5.0"})
            d = _json.load(_ur.urlopen(req, timeout=15))
            ids = {j.get("id") for j in d.get("jobs", [])} |                   {str(j.get("jobUrl", ""))[-36:] for j in d.get("jobs", [])}
            return m.group(2) not in ids
        except _ue.HTTPError as exc:
            # Ashby serves a friendly HTTP-200 "Page not found" shell for the
            # application route even when the underlying board is gone.  The
            # board API is authoritative, so a terminal response is stale, not
            # a retryable resume-upload failure.
            return exc.code in {404, 410}
        except Exception:
            return False
    try:
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=15) as r:
            body = r.read(60000).decode("utf-8", "replace").lower()
        return any(mk in body for mk in DEAD_MARKERS)
    except _ue.HTTPError as exc:
        return exc.code in {404, 410}
    except Exception:
        return False


def _ensure_outcome_columns(conn: sqlite3.Connection) -> None:
    """Add operational outcome fields without requiring a destructive migration."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(postings)")}
    columns = {
        "outcome": "TEXT",
        "last_attempt_at": "INTEGER",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE postings ADD COLUMN {name} {definition}")
    ensure_submission_attempts(conn)
    conn.commit()


def _mark_outcome(conn: sqlite3.Connection, posting_id: str, status: str,
                  outcome: str, reason: str = "", dry_run: bool = False,
                  expected_status: str | None = None, commit: bool = True) -> bool:
    if dry_run:
        return True
    where = "posting_id=?" if expected_status is None else "posting_id=? AND status=?"
    params = (status, outcome, int(time.time()), reason[:1000] or None, posting_id)
    if expected_status is not None:
        params += (expected_status,)
    changed = conn.execute(
        """UPDATE postings
           SET status=?, outcome=?, last_attempt_at=?,
               attempt_count=COALESCE(attempt_count, 0) + 1,
               last_error=?
         WHERE """ + where,
        params,
    ).rowcount
    if commit:
        conn.commit()
    return changed == 1


def _isolated_adapter(payload: dict) -> dict:
    """Run one adapter in a killable process with a hard per-posting deadline."""
    marker = Path(tempfile.gettempdir()) / (
        f"jobhunt-submit-{os.getpid()}-{time.time_ns()}.attempted"
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "submit_worker.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
            env={
                **os.environ,
                "JOBHUNT_PLAYWRIGHT_TIMEOUT_MS": str(PLAYWRIGHT_TIMEOUT_MS),
                "JOBHUNT_SUBMISSION_ATTEMPT_MARKER": str(marker),
            },
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(
                json.dumps(payload), timeout=POSTING_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return {
                "outcome": "manual",
                "ok": False,
                "submitted": False,
                "retryable": False,
                "click_attempted": marker.is_file(),
                "submission_uncertain": True,
                "reason": f"posting timeout after {POSTING_TIMEOUT_SECONDS}s; verify possible prior submission",
            }

        if proc.returncode != 0:
            return {
                "outcome": "manual",
                "ok": False,
                "submitted": False,
                "retryable": False,
                "click_attempted": marker.is_file(),
                "submission_uncertain": True,
                "reason": f"worker exited {proc.returncode}: {stderr[-500:]}; verify possible prior submission",
            }
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {
                "outcome": "manual",
                "ok": False,
                "submitted": False,
                "retryable": False,
                "click_attempted": marker.is_file(),
                "submission_uncertain": True,
                "reason": f"worker returned invalid JSON: {stderr[-500:]}; verify possible prior submission",
            }
        attempted = marker.is_file() or bool(result.get("click_attempted"))
        return _enforce_submission_safety(result, attempted)
    finally:
        marker.unlink(missing_ok=True)


def _enforce_submission_safety(result: dict, attempted: bool) -> dict:
    """Quarantine ambiguity while preserving explicit external rejections."""
    result["click_attempted"] = attempted
    if attempted and not result.get("submitted"):
        if result.get("definitive_rejection"):
            result.update(
                outcome="manual",
                retryable=False,
                submission_uncertain=False,
            )
        else:
            result.update(
                outcome="manual",
                retryable=False,
                submission_uncertain=True,
            )
            reason = str(result.get("reason") or "adapter stopped after submit click")
            if "verify" not in reason.lower():
                result["reason"] = reason + "; verify possible prior submission"
    return result


def _outcome(result: dict) -> str:
    if result.get("outcome") in {"submitted", "manual", "stale", "retryable_failure", "failed"}:
        return result["outcome"]
    if result.get("submitted"):
        return "submitted"
    if result.get("ok") and result.get("unanswered"):
        return "manual"
    if result.get("retryable"):
        return "retryable_failure"
    return "failed"


def _send_notice(subject: str, body: str) -> bool:
    """Best-effort notification after durable state has already been recorded."""
    try:
        import mailer
        mailer.send(subject, body)
        return True
    except Exception as exc:
        print(f"[submit] notification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _posting_already_applied(
    conn: sqlite3.Connection, posting_id: str, url: str
) -> bool:
    return posting_already_applied(conn, posting_id, url)


def _resume_quality_ready(resume_pdf: Path, posting_id: str) -> tuple[bool, str]:
    """Fail closed unless the exact resume artifact passed deterministic review."""
    if not resume_pdf.is_file():
        return False, "resume PDF is missing"
    quality_path = resume_pdf.with_suffix(".quality.json")
    try:
        quality = json.loads(quality_path.read_text())
    except (OSError, ValueError, TypeError):
        return False, "resume quality metadata is missing or invalid"
    checks = (
        (quality.get("posting_id") == posting_id, "quality metadata belongs to another posting"),
        (quality.get("source") == "deterministic_grounded", "resume source is not deterministic"),
        (quality.get("review_required") is False, "resume is flagged for human review"),
        (quality.get("structural_validation") == "passed", "resume structural validation failed"),
        (quality.get("page_count") == 1, "resume is not verified as exactly one page"),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, ""


def _claim_submission(
    conn: sqlite3.Connection,
    posting_id: str,
    url: str,
    from_status: str = "ready",
) -> str:
    return claim_submission(
        conn, posting_id=posting_id, url=url, from_status=from_status
    )


def submit_ready(limit: int = SUBMISSIONS_PER_RUN, dry_run: bool = False) -> list[dict]:
    # 24/7 (David 2026-08-09): ATS forms don't care what hour they're submitted
    # and speed-to-apply wins. Human-ish pacing between submissions retained.
    # Gaming-defer check REMOVED (David 2026-08-17). It matched the Steam
    # client merely existing (steam_osx idles in the menu bar at login), which
    # silently blocked nearly every submit run for weeks. Adapters run headless.
    from submission.executor import execute_claimed_posting
    from submission.lanes import classify_url

    conn = connect_tracker(DB)
    _ensure_outcome_columns(conn)
    rows = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) "
        "WHERE p.status='ready' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id) "
        "ORDER BY p.rowid DESC").fetchall()
    results: list[dict] = []
    attempted = 0
    run_started = time.monotonic()
    try:
        for r in rows:
            if attempted >= limit or (dry_run and len(results) >= limit):
                break
            if time.monotonic() - run_started > RUN_BUDGET_SECONDS:
                print(
                    f"[submit] run budget ({RUN_BUDGET_SECONDS}s) spent — "
                    f"{attempted} attempted; leaving the rest for the next timer run"
                )
                break
            _ats, lane = classify_url(r["url"])
            if lane.name == "ashby":
                continue

            # Rows are selected as a batch, so a prior mirror row in this same run may
            # have just created a canonical application ledger entry. Re-check before
            # claiming and touching an external form.
            if _posting_already_applied(conn, r["posting_id"], r["url"]):
                if not dry_run:
                    conn.execute(
                        "UPDATE postings SET status='filtered_out', outcome='stale', "
                        "last_error='canonical posting already has an application' "
                        "WHERE posting_id=? AND status='ready'",
                        (r["posting_id"],),
                    )
                    conn.commit()
                results.append({
                    "company": r["company"],
                    "ats": "unknown",
                    "outcome": "stale",
                    "reason": "canonical posting already has an application; not resubmitted",
                })
                continue

            if not dry_run:
                claim = _claim_submission(conn, r["posting_id"], r["url"])
                if claim in {"already_applied", "posting_claimed"}:
                    conn.execute(
                        "UPDATE postings SET status='filtered_out', outcome='stale', "
                        "last_error='canonical posting already applied or being submitted' "
                        "WHERE posting_id=? AND status='ready'",
                        (r["posting_id"],),
                    )
                    conn.commit()
                    results.append({
                        "company": r["company"],
                        "ats": "unknown",
                        "outcome": "stale",
                        "reason": "canonical posting already applied or being submitted; not resubmitted",
                    })
                    continue
                if claim != "claimed":
                    continue

            result = execute_claimed_posting(
                conn,
                r,
                lane=lane,
                dry_run=dry_run,
                worker_id=f"submit-{lane.name}",
            )
            results.append(result)
            attempted += 1

            if not dry_run and attempted < limit:
                time.sleep(random.uniform(PACING_MIN_SECONDS, PACING_MAX_SECONDS))
    finally:
        conn.close()
    return results

def main(argv: list[str] | None = None) -> None:
    argv = sys.argv if argv is None else argv
    # rotate old screenshots (14d) so out/ doesn't grow unbounded (32MB after day 1)
    try:
        shots = ROOT / "out" / "screenshots"
        cutoff_ts = time.time() - 14 * 86400
        for f in shots.glob("*.png"):
            if f.stat().st_mtime < cutoff_ts:
                f.unlink()
    except Exception:
        pass
    dry = _dry_run_requested(argv)
    for r in submit_ready(dry_run=dry):
        print(r)


if __name__ == "__main__":
    main()
