"""Cross-process submission point-of-no-return tracking.

Every live ATS adapter records an attempt immediately before it clicks a final
Submit/Send control. The parent process can then quarantine crashes and timeouts
that happen after that point instead of automatically retrying a possibly
successful external application.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


MARKER_ENV = "JOBHUNT_SUBMISSION_ATTEMPT_MARKER"

_CONFIRMATION_RE = re.compile(
    r"(?:thank you for (?:applying|your application)|"
    r"(?:we(?:'ve| have) )?received your application|"
    r"your application (?:has been )?(?:received|submitted|sent)|"
    r"(?:application|connection) (?:has been )?(?:received|submitted|sent)(?: successfully)?)",
    re.IGNORECASE,
)
_CONFIRMATION_URL_RE = re.compile(
    r"(?:confirmation|thank[-_]?you|application[-_]?submitted)",
    re.IGNORECASE,
)


def mark_submit_attempted() -> bool:
    """Persist the point-of-no-return marker, if the parent configured one."""
    value = os.environ.get(MARKER_ENV, "").strip()
    if not value:
        return False
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time():.6f}\n")
    return True


def submit_was_attempted() -> bool:
    value = os.environ.get(MARKER_ENV, "").strip()
    return bool(value and Path(value).is_file())


def confirmation_observed(body_text: str, url: str = "") -> bool:
    """Require an application-specific confirmation, not generic page copy."""
    return bool(
        _CONFIRMATION_RE.search(body_text or "")
        or _CONFIRMATION_URL_RE.search(url or "")
    )


def mark_unconfirmed(result: dict, reason: str = "submit clicked but confirmation was not observed") -> None:
    """Quarantine an attempted submission whose external outcome is unknown."""
    result.update(
        ok=False,
        submitted=False,
        outcome="manual",
        retryable=False,
        click_attempted=True,
        submission_uncertain=True,
        reason=reason,
    )
