"""Run one ATS adapter in its own process.

The parent submitter kills this process when a posting exceeds its deadline, so a
hung browser or adapter cannot block later postings in the queue.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "apply"), str(ROOT / "notify"), str(ROOT / "watcher")]


def _adapter(ats: str, url: str):
    from jd import canonical_application_url, detect_ats
    from submission.lanes import lane_for

    target_url = canonical_application_url(url)
    detected = ats if ats and ats != "other" else detect_ats(target_url)
    if lane_for(detected).name == "unsupported":
        return None, False, detected, target_url
    if detected == "greenhouse":
        from greenhouse import apply_greenhouse
        return apply_greenhouse, False, detected, target_url
    if detected == "lever":
        from lever import apply_lever
        return apply_lever, False, detected, target_url
    if detected == "ashby":
        from ashby import apply_ashby
        return apply_ashby, False, detected, target_url
    if detected == "workday":
        from workday import apply_workday
        return apply_workday, False, detected, target_url
    if detected == "smartrecruiters":
        from smartrecruiters import apply_smartrecruiters
        return apply_smartrecruiters, False, detected, target_url
    if detected == "rippling":
        from rippling import apply_rippling
        return apply_rippling, False, detected, target_url
    if detected == "workable":
        from workable import apply_workable
        return apply_workable, False, detected, target_url
    if "workatastartup.com" in url:
        from waas import apply_waas
        return apply_waas, True, "waas", target_url
    return None, False, detected, target_url


def main() -> int:
    payload = json.load(sys.stdin)
    url = str(payload["url"])
    ats = str(payload.get("ats") or "")
    slug = str(payload["slug"])
    pdf = Path(str(payload["resume_pdf"]))
    dry_run = bool(payload.get("dry_run", False))
    # Track-based graduation (David 2026-08-19): expose the job title before
    # any adapter (and therefore qa.py) is imported, so the per-process
    # overlay in qa.py resolves intern vs full-time for THIS posting.
    os.environ["JOBHUNT_JOB_TITLE"] = str(payload.get("title") or "")
    fn, waas, detected, target_url = _adapter(ats, url)
    if fn is None:
        print(json.dumps({"outcome": "manual", "ok": False, "submitted": False,
                          "reason": f"no adapter for {detected}"}))
        return 0

    try:
        # Adapters are allowed to log diagnostics, but stdout remains one JSON line
        # for the parent process to parse reliably.
        with contextlib.redirect_stdout(sys.stderr), contextlib.redirect_stderr(sys.stderr):
            if waas:
                result = fn(target_url, slug, dry_run=dry_run)
            else:
                result = fn(target_url, pdf, slug, dry_run=dry_run)
        result = dict(result or {})
        result.setdefault("ok", False)
        result.setdefault("submitted", False)
        result["detected_ats"] = detected
        print(json.dumps(result, default=str))
        return 0
    except Exception as exc:
        from submission_state import submit_was_attempted
        attempted = submit_was_attempted()
        print(json.dumps({
            "outcome": "manual" if attempted else "retryable_failure",
            "ok": False,
            "submitted": False,
            "retryable": not attempted,
            "click_attempted": attempted,
            "submission_uncertain": attempted,
            "reason": f"adapter crash: {type(exc).__name__}: {exc}",
            "detected_ats": detected,
        }))
        return 0


if __name__ == "__main__":
    started = time.monotonic()
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            from submission_state import submit_was_attempted
            attempted = submit_was_attempted()
        except Exception:
            attempted = False
        print(json.dumps({
            "outcome": "manual" if attempted else "retryable_failure",
            "ok": False,
            "submitted": False,
            "retryable": not attempted,
            "click_attempted": attempted,
            "submission_uncertain": attempted,
            "reason": f"worker crash: {type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - started, 3),
        }))
        raise SystemExit(0)
