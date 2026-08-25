from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Iterable, List, Optional

from compensation.models import CompensationResolution, EvidenceObservation, JobContext
from compensation.normalize import canonical_domain

_SECONDS_30_DAYS = 30 * 86400
_HOURS_PER_YEAR = Decimal("2080")
_GENERIC_ROLE_TOKENS = {"software", "engineer"}
_GENERIC_LOCATION_TOKENS = {"ny", "usa", "us"}


def _tokens(value: str) -> List[str]:
    return [token for token in "".join(ch.lower() if ch.isalnum() else " " for ch in value).split() if token]


def _contains_all_tokens(needles: List[str], haystack: List[str]) -> bool:
    if not needles:
        return True
    haystack_set = set(haystack)
    return all(token in haystack_set for token in needles)


def _compatible(context: JobContext, observation: EvidenceObservation) -> bool:
    if observation.currency != context.currency:
        return False

    obs_tokens = _tokens(" ".join([observation.title, observation.domain, observation.url]))
    company_tokens = _tokens(context.company)
    if company_tokens and not _contains_all_tokens(company_tokens, obs_tokens):
        return False

    role_tokens = [token for token in _tokens(context.title) if token not in _GENERIC_ROLE_TOKENS]
    title_tokens = _tokens(observation.title)
    if role_tokens and not _contains_all_tokens(role_tokens, title_tokens):
        return False

    if observation.source_kind != "employer":
        location_tokens = [token for token in _tokens(context.location) if token not in _GENERIC_LOCATION_TOKENS]
        if location_tokens and not _contains_all_tokens(location_tokens, obs_tokens):
            return False

    return True


def _valid_bounds(low: Decimal, high: Decimal, period: str) -> bool:
    if low <= 0 or high <= 0 or low > high:
        return False
    if period == "hour":
        return Decimal("5") <= low <= Decimal("500") and Decimal("5") <= high <= Decimal("500")
    if period == "year":
        return Decimal("10000") <= low <= Decimal("1000000") and Decimal("10000") <= high <= Decimal("1000000")
    return False


def _estimate(observation: EvidenceObservation) -> Optional[Decimal]:
    if observation.point is not None:
        value = observation.point
    elif observation.low is not None and observation.high is not None:
        if not _valid_bounds(observation.low, observation.high, observation.period):
            return None
        value = (observation.low + observation.high) / Decimal("2")
    else:
        return None
    if value <= 0:
        return None
    return value


def _convert(value: Decimal, source_period: str, target_period: str) -> Optional[Decimal]:
    if source_period == target_period:
        return value
    if source_period == "hour" and target_period == "year":
        return value * _HOURS_PER_YEAR
    if source_period == "year" and target_period == "hour":
        return value / _HOURS_PER_YEAR
    return None


def _round(value: Decimal, period: str) -> Decimal:
    if period == "year":
        return (value / Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("1000")
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _resolution(context: JobContext, amount: Decimal, method: str, evidence: Iterable[EvidenceObservation], now: int) -> CompensationResolution:
    return CompensationResolution(
        context=context,
        amount=amount,
        method=method,
        evidence=tuple(evidence),
        researched_at=now,
        expires_at=now + _SECONDS_30_DAYS,
    )


def _domain_key(observation: EvidenceObservation) -> str:
    return canonical_domain(observation.domain)


def resolve_observations(context: JobContext, observations: Iterable[EvidenceObservation], *, now: int) -> Optional[CompensationResolution]:
    compatible = [row for row in observations if _compatible(context, row)]

    employer_ranges = [
        row for row in compatible
        if row.source_kind == "employer" and row.low is not None and row.high is not None and row.period == context.period
    ]
    if len(employer_ranges) > 1:
        return None
    if len(employer_ranges) == 1:
        row = employer_ranges[0]
        if not _valid_bounds(row.low, row.high, row.period):
            return None
        amount = _round((row.low + row.high) / Decimal("2"), context.period)
        return _resolution(context, amount, "employer_midpoint", [row], now)

    by_domain = {}
    for row in compatible:
        if row.source_kind != "market":
            continue
        estimate = _estimate(row)
        if estimate is None:
            continue
        converted = _convert(estimate, row.period, context.period)
        if converted is None:
            continue
        domain = _domain_key(row)
        if domain not in by_domain:
            by_domain[domain] = (converted, row)

    if len(by_domain) < 2:
        return None

    values = [value for value, _row in by_domain.values()]
    if min(values) <= 0 or max(values) / min(values) > Decimal("2"):
        return None

    amount = _round(Decimal(str(median(values))), context.period)
    evidence = [row for _value, row in by_domain.values()]
    return _resolution(context, amount, "market_median", evidence, now)
