"""Fetch job description text for a posting URL. ATS-aware with generic fallback."""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse
import urllib.request

from apply.oraclecloud_url import parse_oracle_posting_url

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _greenhouse_board_slug(page_html: str) -> str | None:
    """Extract the employer board required by Greenhouse's migrated embed URL."""
    decoded = htmllib.unescape(page_html)
    patterns = (
        r"greenhouse\.io/(?:embed/)?job_board(?:/js)?\?[^\"']*\bfor=([A-Za-z0-9_-]+)",
        r"job-boards\.greenhouse\.io/([A-Za-z0-9_-]+)/(?:jobs|embed)/",
        r"boards\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, re.I)
        if match:
            return match.group(1)
    return None


def canonical_application_url(url: str) -> str:
    """Resolve supported ATS wrappers to the application URL the adapter expects.

    Several employers keep their public careers URL while embedding a Greenhouse
    application identified by ``gh_jid``.  The numeric token is sufficient to use
    Greenhouse's current embedded application endpoint. Greenhouse now requires
    the employer board slug as well as the numeric token, so wrapper pages are
    inspected for the official job-board script when necessary.
    """
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    ashby_ids = query.get("ashby_jid", [])
    if ashby_ids and re.fullmatch(r"[0-9a-f-]{36}", ashby_ids[0], re.I):
        labels = [part for part in parsed.netloc.lower().split(".")
                  if part not in {"www", "jobs", "careers"}]
        if labels:
            org = urllib.parse.quote(labels[0], safe="")
            job_id = urllib.parse.quote(ashby_ids[0], safe="")
            return f"https://jobs.ashbyhq.com/{org}/{job_id}"

    greenhouse_ids = query.get("gh_jid", [])
    if greenhouse_ids and re.fullmatch(r"\d+", greenhouse_ids[0]):
        token = urllib.parse.quote(greenhouse_ids[0], safe="")
        try:
            board = _greenhouse_board_slug(_get(url))
        except Exception:
            board = None
        if board:
            board = urllib.parse.quote(board, safe="")
            return (
                "https://job-boards.greenhouse.io/embed/job_app"
                f"?for={board}&token={token}"
            )
        # Preserve the previous token-only route as a compatibility fallback.
        return f"https://boards.greenhouse.io/embed/job_app?token={token}"

    if parsed.netloc.lower().removeprefix("www.") == "janestreet.com":
        match = re.search(r"/(\d{7,})/?$", parsed.path)
        if match:
            return (
                "https://job-boards.greenhouse.io/embed/job_app"
                f"?for=janestreet&token={match.group(1)}"
            )
    return url


def detect_ats(url: str) -> str:
    url = canonical_application_url(url)
    if parse_oracle_posting_url(url):
        return "oraclecloud"
    host = urllib.parse.urlparse(url).netloc.lower()
    if "greenhouse.io" in host: return "greenhouse"
    if "lever.co" in host: return "lever"
    if "ashbyhq.com" in host: return "ashby"
    if "myworkdayjobs.com" in host: return "workday"
    if "icims.com" in host: return "icims"
    if "smartrecruiters.com" in host: return "smartrecruiters"
    if "ats.rippling.com" in host: return "rippling"
    if "apply.workable.com" in host: return "workable"
    return "other"


def _greenhouse(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    board = (query.get("for") or [None])[0]
    token = (query.get("token") or [None])[0]
    if board and token and re.fullmatch(r"\d+", token):
        api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{token}"
        d = json.loads(_get(api))
        return _strip_html(htmllib.unescape(d.get("content", "")))
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?[^#]*token=(\d+)|([^/]+)/jobs/(\d+))", url)
    if m and m.group(2):
        api = f"https://boards-api.greenhouse.io/v1/boards/{m.group(2)}/jobs/{m.group(3)}"
        d = json.loads(_get(api))
        return _strip_html(htmllib.unescape(d.get("content", "")))
    raise ValueError("unrecognized greenhouse url")


def _lever(url: str) -> str:
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError("unrecognized lever url")
    d = json.loads(_get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}"))
    parts = [d.get("descriptionPlain", "")]
    for lst in d.get("lists", []):
        parts.append(lst.get("text", ""))
        parts.append(_strip_html(lst.get("content", "")))
    return "\n".join(p for p in parts if p)


def _ashby(url: str) -> str:
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError("unrecognized ashby url")
    org, job_id = m.group(1), m.group(2)
    d = json.loads(_get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"))
    for j in d.get("jobs", []):
        if job_id in (j.get("id"), j.get("jobUrl", "")) or job_id in j.get("applyUrl", ""):
            return _strip_html(j.get("descriptionHtml", "")) or j.get("descriptionPlain", "")
    raise ValueError("job not in ashby board feed")


def _workday(url: str) -> str:
    u = urllib.parse.urlparse(url)
    tenant = u.netloc.split(".")[0]
    segs = [s for s in u.path.split("/") if s]
    # path: [locale?] site / job / location / slug
    if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
        segs = segs[1:]
    site = segs[0]
    job_path = "/".join(segs[segs.index("job") + 1:])
    api = f"https://{u.netloc}/wday/cxs/{tenant}/{site}/job/{job_path}"
    d = json.loads(_get(api))
    info = d.get("jobPostingInfo", {})
    return _strip_html(info.get("jobDescription", ""))


def fetch_jd(url: str) -> str:
    """Best-effort JD text. Returns '' on total failure (tailor still works, generic)."""
    url = canonical_application_url(url)
    ats = detect_ats(url)
    try:
        if ats == "greenhouse": return _greenhouse(url)[:12000]
        if ats == "lever": return _lever(url)[:12000]
        if ats == "ashby": return _ashby(url)[:12000]
        if ats == "workday": return _workday(url)[:12000]
        return _strip_html(_get(url))[:12000]
    except Exception:
        try:
            return _strip_html(_get(url))[:12000]
        except Exception:
            return ""


if __name__ == "__main__":
    import sys
    u = sys.argv[1]
    print(f"ats={detect_ats(u)}")
    print(fetch_jd(u)[:500])
