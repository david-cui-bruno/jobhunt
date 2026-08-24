from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


_ORACLE_PATH_RE = re.compile(
    r"^/hcmUI/CandidateExperience/(?P<locale>[^/]+)/sites/(?P<site>[^/]+)/job/(?P<job_id>\d+)/?$",
    re.I,
)


@dataclass(frozen=True)
class OraclePostingUrl:
    host: str
    locale: str
    site: str
    job_id: str


def parse_oracle_posting_url(url: str) -> OraclePostingUrl | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    match = _ORACLE_PATH_RE.fullmatch(parsed.path)
    if parsed.scheme != "https" or not host.endswith(".oraclecloud.com") or not match:
        return None
    return OraclePostingUrl(host, match["locale"], match["site"], match["job_id"])
