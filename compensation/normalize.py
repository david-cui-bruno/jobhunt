import re
import unicodedata
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

_CURRENCY_CODES = set("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC
CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS
KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK
MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB
PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE
SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH
UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC
XBD XCD XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWL
""".split())
_FOREIGN_CODES = {code.lower() for code in _CURRENCY_CODES if code != "USD"}
_COMMON_FOREIGN_CODES = {"cad", "aud", "nzd", "hkd", "mxn", "gbp", "eur", "jpy", "cny", "inr", "chf"}
_FOREIGN_WORDS = {
    "canadian dollars",
    "australian dollars",
    "new zealand dollars",
    "hong kong dollars",
    "mexican pesos",
    "british pounds",
    "euros",
    "euro",
    "pounds",
    "yen",
    "yuan",
    "rupees",
    "swiss francs",
    "au dollars",
}


def canonical_domain(value: str) -> str:
    host = urlparse(value).netloc.lower() or value.lower()
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


def _has_disqualifying_currency_qualifier(text: str, start: int, end: int, matched: str) -> bool:
    prefix = text[max(0, start - 48):start]
    suffix = text[end:end + 16]
    nearby = prefix + matched
    for char in nearby + suffix:
        if unicodedata.category(char) == "Sc" and char != "$":
            return True

    qualifier = prefix.strip().split()[-1] if prefix.strip().split() else ""
    qualifier = qualifier.rstrip("$")
    if re.fullmatch(r"[A-Z]{1,3}", qualifier) and qualifier != "USD":
        return True
    if qualifier.lower() in _COMMON_FOREIGN_CODES:
        return True

    adjacent = text[max(0, start - 4):end + 8]
    if re.search(r"\b(?:%s)\b" % "|".join(sorted(_COMMON_FOREIGN_CODES)), adjacent, re.I):
        return True
    if re.match(r"[A-Z]{1,3}\$", adjacent) and not adjacent.startswith("USD"):
        return True

    lowered_context = (prefix + suffix).lower()
    if any(word in lowered_context for word in _FOREIGN_WORDS):
        return True

    return False


def _has_disqualifying_currency_label(question: str) -> bool:
    if any(unicodedata.category(char) == "Sc" and char != "$" for char in question):
        return True

    for match in re.finditer(r"\b([A-Za-z]{1,3})\$", question):
        if match.group(1).lower() not in {"us", "usd"}:
            return True

    code_patterns = (
        r"\(([A-Z]{3})\)",
        r"\b(?:in|currency(?:\s+is)?|denominated\s+in|paid\s+in)\s*[:=]?\s*([A-Z]{3})\b",
        r"\b([A-Z]{3})\s+(?:salary|compensation|pay|rate)\b",
    )
    for pattern in code_patterns:
        for match in re.finditer(pattern, question, re.I):
            if match.group(1).lower() in _FOREIGN_CODES:
                if re.match(r"\s+(?:role|position|job|developer|engineer)\b", question[match.end():], re.I):
                    continue
                return True

    lowered = question.lower()
    if re.search(r"\b(?:%s)\b" % "|".join(sorted(_COMMON_FOREIGN_CODES)), lowered):
        return True
    return any(word in lowered for word in _FOREIGN_WORDS)


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
        if _has_disqualifying_currency_qualifier(text, match.start(), match.end(), matched):
            continue
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
                domain=canonical_domain(url),
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
    if _has_disqualifying_currency_label(question):
        return None
    if any(token in lowered for token in (
        "monthly", "per month", "/month", "weekly", "per week", "/week",
        "fortnightly", "per fortnight", "/fortnight", "biweekly", "per two weeks",
        "daily", "per day", "/day",
    )):
        return None
    if any(token in lowered for token in ("hourly", "per hour", "/hr", "hour rate", "hourly rate", "requested rate")):
        return "hour"
    if any(token in lowered for token in ("annual", "yearly", "per year", "base salary", "salary")):
        return "year"
    return None
