"""Application track inference: internship vs full-time.

David 2026-08-19: graduation is track-dependent from now on.
  - internship applications: expected graduation May 2028 (exact day 05/15/2028)
  - full-time applications:  expected graduation May 2027 (exact day 05/15/2027)
He confirmed the early-graduation plan is real (he would actually graduate a
year early to take a full-time job), so the 2027 date survives a background
check. Both tailor/ (resume PDF) and apply/qa.py (form answers) must agree on
the track for one application, so the classifier lives here and nowhere else.

The title is the signal: 'intern'/'internship'/'co-op' anywhere in the title
means intern; everything else (including 'new grad', 'university grad',
'founding engineer') is full-time. Unknown/empty titles default to intern,
because the bulk of the pipeline is internship lists and the intern date never
overstates availability.
"""
from __future__ import annotations

import os
import re

INTERN_RE = re.compile(r"\bintern(?:ship)?\b|\bco[- ]?op\b", re.I)

GRAD_MONTH = "May"
GRAD_YEAR = {"intern": "2028", "fulltime": "2027"}
GRAD_EXACT_DAY = "15"  # user-approved estimate for forms that require a day

ENV_TITLE = "JOBHUNT_JOB_TITLE"
ENV_TRACK = "JOBHUNT_JOB_TRACK"


def infer_track(title: str | None) -> str:
    """-> 'intern' | 'fulltime' from a job title."""
    return "intern" if INTERN_RE.search(str(title or "")) else "fulltime"


def current_track(title: str | None = None) -> str:
    """Track for the application being processed right now.

    Priority: explicit title arg -> JOBHUNT_JOB_TRACK env (set by
    submit_worker from the posting row) -> JOBHUNT_JOB_TITLE env -> intern.
    """
    if title:
        return infer_track(title)
    env_track = os.environ.get(ENV_TRACK, "").strip().lower()
    if env_track in ("intern", "fulltime"):
        return env_track
    env_title = os.environ.get(ENV_TITLE, "").strip()
    if env_title:
        return infer_track(env_title)
    return "intern"


def grad_year(track: str) -> str:
    return GRAD_YEAR.get(track, GRAD_YEAR["intern"])


def grad_month_year(track: str) -> str:
    return f"{GRAD_MONTH} {grad_year(track)}"


def grad_exact_date(track: str) -> str:
    y = grad_year(track)
    return f"05/{GRAD_EXACT_DAY}/{y}"
