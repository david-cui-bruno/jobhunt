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
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "apply"), str(ROOT / "notify")]

DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")
SUBMISSIONS_PER_RUN = int(os.environ.get("JOBHUNT_SUBMISSIONS_PER_RUN", "8"))
PACING_MIN_SECONDS = float(os.environ.get("JOBHUNT_PACING_MIN_SECONDS", "15"))
PACING_MAX_SECONDS = float(os.environ.get("JOBHUNT_PACING_MAX_SECONDS", "45"))
POSTING_TIMEOUT_SECONDS = int(os.environ.get("JOBHUNT_POSTING_TIMEOUT_SECONDS", "300"))
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
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "submit_worker.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "JOBHUNT_PLAYWRIGHT_TIMEOUT_MS": str(PLAYWRIGHT_TIMEOUT_MS)},
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
            "outcome": "retryable_failure",
            "ok": False,
            "submitted": False,
            "retryable": True,
            "submission_uncertain": True,
            "reason": f"posting timeout after {POSTING_TIMEOUT_SECONDS}s",
        }

    if proc.returncode != 0:
        return {
            "outcome": "retryable_failure",
            "ok": False,
            "submitted": False,
            "retryable": True,
            "submission_uncertain": True,
            "reason": f"worker exited {proc.returncode}: {stderr[-500:]}",
        }
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "outcome": "retryable_failure",
            "ok": False,
            "submitted": False,
            "retryable": True,
            "submission_uncertain": True,
            "reason": f"worker returned invalid JSON: {stderr[-500:]}",
        }
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


def submit_ready(limit: int = SUBMISSIONS_PER_RUN, dry_run: bool = False) -> list[dict]:
    # 24/7 (David 2026-08-09): ATS forms don't care what hour they're submitted
    # and speed-to-apply wins. Human-ish pacing between submissions retained.
    if _user_is_gaming():
        print("[submit] deferring: game/fullscreen app active")
        return []

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    _ensure_outcome_columns(conn)
    rows = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) "
        "WHERE p.status='ready' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id) "
        "ORDER BY p.rowid DESC").fetchall()
    results = []
    done = 0
    from drip import claim_posting
    for r in rows:
        if done >= limit or (dry_run and len(results) >= limit):
            break
        slug = f"{r['company'].replace(' ', '_')[:40]}_{int(time.time())}"
        pdf = _runtime_path(r["resume_pdf"])
        if not dry_run and not claim_posting(
                conn, r["posting_id"], "submitting", from_status="ready"):
            continue
        if _posting_dead(r["url"]):
            _mark_outcome(conn, r["posting_id"], "filtered_out", "stale",
                          "liveness check marked posting stale", dry_run,
                          expected_status=None if dry_run else "submitting")
            results.append({"company": r["company"], "ats": "unknown", "outcome": "stale"})
            continue

        try:
            res = _isolated_adapter({
                "url": r["url"],
                "resume_pdf": str(pdf),
                "slug": slug,
                "dry_run": dry_run,
            })
        except Exception as exc:
            res = {
                "outcome": "retryable_failure",
                "ok": False,
                "submitted": False,
                "submission_uncertain": True,
                "reason": f"adapter launch failed: {type(exc).__name__}: {exc}",
            }
        outcome = _outcome(res)
        # retryable (timeouts, network blips): stay 'ready' so the next hourly
        # sweep retries automatically, up to 3 attempts, then settle failed.
        # (Before this, retryable_failure settled 'failed' and was never retried;
        # Kastle and Scale both needed manual requeues on 2026-08-08.)
        attempts = (r["attempt_count"] or 0) if "attempt_count" in r.keys() else 0
        if res.get("submission_uncertain"):
            status_for_outcome = "manual"
        elif outcome == "retryable_failure" and attempts < 2:
            status_for_outcome = "ready"
        else:
            status_for_outcome = {
                "submitted": "submitted",
                "manual": "manual",
                "stale": "filtered_out",
                "retryable_failure": "failed",
                "failed": "failed",
            }.get(outcome, "failed")
        reason = str(res.get("reason", ""))
        changed = _mark_outcome(
            conn, r["posting_id"], status_for_outcome, outcome, reason, dry_run,
            expected_status=None if dry_run else "submitting",
            commit=not (outcome == "submitted" and not dry_run),
        )
        if outcome == "submitted" and not dry_run:
            if not changed:
                conn.rollback()
                results.append({"company": r["company"], "ats": res.get("detected_ats", "unknown"),
                                "outcome": "manual", "reason": "submission claim lost; verify"})
                continue
            ats = str(res.get("detected_ats", "unknown"))
            # Never overwrite a prior submission record. The ready-query anti-join
            # is the first guard; this unique insert closes the race if another
            # worker records the application after rows were selected.
            try:
                conn.execute(
                    "INSERT INTO applications VALUES (?,?,?,?,?,?)",
                    (r["posting_id"], str(pdf), ats, int(time.time()), reason, ""))
            except sqlite3.IntegrityError:
                conn.rollback()
                conn.execute(
                    "UPDATE postings SET status='submitted', outcome='submitted', "
                    "last_error='already present in applications ledger' "
                    "WHERE posting_id=? AND status='submitting'",
                    (r["posting_id"],),
                )
                conn.commit()
                results.append({
                    "company": r["company"],
                    "ats": ats,
                    "outcome": "submitted",
                    "reason": "already present in applications ledger; not resubmitted",
                })
                continue
            conn.commit()
            done += 1
        elif outcome == "manual" and res.get("unanswered") and not dry_run:
            import mailer
            mailer.send(f"[jobhunt] manual input needed: {r['company']}",
                        f"{r['company']} — {r['title']}\n{r['url']}\n\n"
                        f"Auto-fill couldn't answer: {res['unanswered']}\n"
                        "Reply with answers and I'll retry, or apply manually.")
        results.append({"company": r["company"], "ats": res.get("detected_ats", "unknown"),
                        "outcome": outcome, "reason": reason})
        # Keep attempts sequential and lightly staggered without imposing the old
        # one-to-four-minute artificial delay. Do not sleep after reaching the cap.
        if not dry_run and done < limit:
            time.sleep(random.uniform(PACING_MIN_SECONDS, PACING_MAX_SECONDS))
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
