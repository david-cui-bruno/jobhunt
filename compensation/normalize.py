import re
from decimal import Decimal, InvalidOperation
from typing import List, Optional
from urllib.parse import urlparse

from compensation.models import EvidenceObservation

_RANGE = re.compile(
    r"(?:USD\s*)?\$?\s*(?P<low>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?:-|–|—|to)\s*(?:USD\s*)?\$?\s*(?P<high>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<period>per\s+hour|hourly|/\s*hr|per\s+year|annually|annual)",
    re.I,
)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _period(token: str) -> str:
    lowered = token.lower().replace(" ", "")
    if "hour" in lowered or "hr" in lowered:
        return "hour"
    return "year"


def _valid_bounds(low: Decimal, high: Decimal, period: str) -> bool:
    if low <= 0 or high <= 0 or low > high:
        return False
    if period == "hour":
        return Decimal("5") <= low <= Decimal("500") and Decimal("5") <= high <= Decimal("500")
    return Decimal("10000") <= low <= Decimal("1000000") and Decimal("10000") <= high <= Decimal("1000000")


def extract_usd_observations(text: str, *, url: str, title: str, source_kind: str, observed_at: int) -> List[EvidenceObservation]:
    rows = []
    for match in _RANGE.finditer(text):
        matched = match.group(0)
        if "$" not in matched and "USD" not in matched.upper():
            continue
        try:
            low = _decimal(match.group("low"))
            high = _decimal(match.group("high"))
        except InvalidOperation:
            continue
        period = _period(match.group("period"))
        if not _valid_bounds(low, high, period):
            continue
        rows.append(
            EvidenceObservation(
                url=url,
                domain=_domain(url),
                title=title,
                low=low,
                high=high,
                point=None,
                currency="USD",
                period=period,
                source_kind=source_kind,
                observed_at=observed_at,
            )
        )
    return rows


def requested_period(question: str) -> Optional[str]:
    lowered = question.lower()
    if any(token in lowered for token in ("hourly", "per hour", "/hr", "hour rate", "hourly rate", "requested rate")):
        return "hour"
    if any(token in lowered for token in ("annual", "yearly", "per year", "base salary", "salary")):
        return "year"
    return None
