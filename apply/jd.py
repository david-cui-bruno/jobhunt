"""Fetch job description text for a posting URL. ATS-aware with generic fallback."""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse
import urllib.request

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


def detect_ats(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "greenhouse.io" in host: return "greenhouse"
    if "lever.co" in host: return "lever"
    if "ashbyhq.com" in host: return "ashby"
    if "myworkdayjobs.com" in host: return "workday"
    if "icims.com" in host: return "icims"
    if "smartrecruiters.com" in host: return "smartrecruiters"
    return "other"


def _greenhouse(url: str) -> str:
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
