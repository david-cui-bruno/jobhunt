import json
import socket
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Callable, Iterable, List, Optional, Protocol, Sequence
from urllib.parse import urlparse

from apply.jd import fetch_jd as default_fetch_jd
from compensation.models import JobContext
from compensation.normalize import canonical_domain, extract_usd_observations, requested_period
from compensation.resolve import _compatible, resolve_observations
from compensation.schema import ensure_compensation_schema, store_resolution
from track import infer_track

APPROVED_DOMAINS = ("levels.fyi", "indeed.com", "glassdoor.com", "salary.com")
TAVILY_URL = "https://api.tavily.com/search"


class SearchProvider(Protocol):
    def search(self, query: str, include_domains: Sequence[str]) -> List[dict]:
        ...


class SearchProviderUnavailable(Exception):
    pass


class TavilySearchProvider:
    def __init__(self, api_key: str, timeout: int = 15) -> None:
        if not api_key:
            raise SearchProviderUnavailable("TAVILY_API_KEY is required")
        self.api_key = api_key
        self.timeout = timeout

    def search(self, query: str, include_domains: Sequence[str]) -> List[dict]:
        domains = [domain for domain in include_domains if domain in APPROVED_DOMAINS]
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "advanced",
            "include_domains": domains,
            "max_results": 8,
            "include_answer": False,
            "include_raw_content": False,
        }
        request = urllib.request.Request(
            TAVILY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except socket.timeout:
            raise TimeoutError("Tavily request timed out")
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), socket.timeout):
                raise TimeoutError("Tavily request timed out")
            raise SearchProviderUnavailable(str(exc))

        try:
            data = json.loads(body)
        except ValueError:
            return []
        results = data.get("results", []) if isinstance(data, dict) else []
        return _filter_result_dicts(results, domains)


def _approved_https_url(url: str, include_domains: Sequence[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = canonical_domain(url)
    return any(host == domain or host.endswith("." + domain) for domain in include_domains)


def _filter_result_dicts(results: Iterable[object], include_domains: Sequence[str]) -> List[dict]:
    filtered = []
    for row in results:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        title = row.get("title")
        content = row.get("content")
        if not (isinstance(url, str) and isinstance(title, str) and isinstance(content, str)):
            continue
        if not _approved_https_url(url, include_domains):
            continue
        filtered.append({"url": url, "title": title, "content": content})
    return filtered


def _status(value: str, posting_id: str, **extra: object) -> dict:
    result = {"posting_id": posting_id, "status": value}
    result.update(extra)
    return result


def _has_compensation_marker(last_error: str) -> bool:
    lowered = last_error.lower()
    return any(
        token in lowered
        for token in (
            "compensation",
            "salary",
            "hourly",
            "pay range",
            "pay rate",
            "desired rate",
            "requested rate",
        )
    )


def _public_query(row) -> str:
    parts = [row["company"] or "", row["title"] or "", row["locations"] or "", "compensation pay range"]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _context_from_row(row, period: str) -> JobContext:
    return JobContext(
        posting_id=row["posting_id"],
        company=row["company"] or "",
        title=row["title"] or "",
        location=row["locations"] or "",
        employment_type=infer_track(row["title"]),
        currency="USD",
        period=period,
    )


def _summary(resolution) -> dict:
    return {
        "posting_id": resolution.context.posting_id,
        "status": "stored",
        "amount": str(resolution.amount.quantize(Decimal("1"))),
        "period": resolution.context.period,
        "method": resolution.method,
        "source_urls": [row.url for row in resolution.evidence],
    }


def _has_multiple_compatible_employer_ranges(context: JobContext, observations: Iterable[object]) -> bool:
    rows = [
        row for row in observations
        if getattr(row, "source_kind", None) == "employer" and _compatible(context, row)
    ]
    return len(rows) > 1


def prepare_posting(
    conn,
    posting_id: str,
    provider: Optional[SearchProvider],
    *,
    now: int,
    fetch_jd: Callable[[str], str] = default_fetch_jd,
) -> dict:
    ensure_compensation_schema(conn)
    row = conn.execute(
        "SELECT posting_id, company, title, locations, url, status, last_error FROM postings WHERE posting_id=?",
        (posting_id,),
    ).fetchone()
    if row is None:
        return _status("posting_not_found", posting_id)

    status = (row["status"] or "").lower()
    last_error = row["last_error"] or ""
    if status not in ("manual", "failed"):
        return _status("not_manual_or_failed", posting_id)
    if not _has_compensation_marker(last_error):
        return _status("not_compensation_blocker", posting_id)

    period = requested_period(last_error)
    if period is None:
        return _status("manual_period_unknown", posting_id)

    context = _context_from_row(row, period)
    observations = []
    url = row["url"] or ""
    try:
        jd_text = fetch_jd(url) or ""
    except Exception:
        jd_text = ""
    if url and jd_text:
        observations.extend(
            extract_usd_observations(jd_text, url=url, title=context.title, source_kind="employer", observed_at=now)
        )
    if _has_multiple_compatible_employer_ranges(context, observations):
        return _status("ambiguous_employer_evidence", posting_id)
    resolution = resolve_observations(context, observations, now=now)
    if resolution is not None:
        store_resolution(conn, resolution)
        return _summary(resolution)

    if provider is None:
        return _status("search_provider_unavailable", posting_id)

    try:
        raw_results = provider.search(_public_query(row), APPROVED_DOMAINS)
    except TimeoutError:
        return _status("search_timeout", posting_id)
    except SearchProviderUnavailable:
        return _status("search_provider_unavailable", posting_id)
    except Exception:
        return _status("search_failed", posting_id)

    for result in _filter_result_dicts(raw_results, APPROVED_DOMAINS):
        observations.extend(
            extract_usd_observations(
                result["content"],
                url=result["url"],
                title=result["title"],
                source_kind="market",
                observed_at=now,
            )
        )
    resolution = resolve_observations(context, observations, now=now)
    if resolution is None:
        return _status("insufficient_evidence", posting_id)
    store_resolution(conn, resolution)
    return _summary(resolution)


def prepare_pending_compensation(conn, provider: SearchProvider, *, limit: int = 5, now: Optional[int] = None) -> dict:
    ensure_compensation_schema(conn)
    effective_now = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        """
        SELECT posting_id FROM postings
        WHERE status IN ('manual','failed')
          AND (
            lower(COALESCE(last_error,'')) GLOB '*compensation*'
            OR lower(COALESCE(last_error,'')) GLOB '*salary*'
            OR lower(COALESCE(last_error,'')) GLOB '*pay*'
          )
        ORDER BY COALESCE(last_attempt_at, first_seen), posting_id
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    summary = {"examined": 0, "stored": 0, "manual": 0, "errors": 0, "limit": int(limit)}
    for row in rows:
        posting_id = row["posting_id"] if hasattr(row, "keys") else row[0]
        summary["examined"] += 1
        try:
            result = prepare_posting(conn, posting_id, provider, now=effective_now, fetch_jd=default_fetch_jd)
        except Exception:
            summary["errors"] += 1
            continue
        status = result.get("status")
        if status == "stored":
            summary["stored"] += 1
        elif status in {"search_failed", "search_timeout", "search_provider_unavailable"}:
            summary["errors"] += 1
        else:
            summary["manual"] += 1
    return summary
