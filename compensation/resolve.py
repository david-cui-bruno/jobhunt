from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Iterable, List, Optional, Tuple

from compensation.models import CompensationResolution, EvidenceObservation, JobContext

_SECONDS_30_DAYS = 30 * 86400
_HOURS_PER_YEAR = Decimal("2080")


def _tokens(value: str) -> List[str]:
    return [token for token in "".join(ch.lower() if ch.isalnum() else " " for ch in value).split() if token]


def _compatible(context: JobContext, observation: EvidenceObservation) -> bool:
    if observation.currency != context.currency:
        return False
    title_tokens = set(_tokens(observation.title))
    if context.company and context.company.lower() not in (observation.title + " " + observation.domain).lower():
        return False
    role_tokens = [token for token in _tokens(context.title) if token not in {"software", "engineer"}]
    if role_tokens and not any(token in title_tokens for token in role_tokens):
        return False
    if observation.source_kind != "employer":
        location_tokens = [token for token in _tokens(context.location) if token not in {"ny", "usa", "us"}]
        obs_blob = (observation.title + " " + observation.url).lower()
        if location_tokens and not any(token in obs_blob for token in location_tokens):
            return False
    return True


def _estimate(observation: EvidenceObservation) -> Optional[Decimal]:
    if observation.point is not None:
        value = observation.point
    elif observation.low is not None and observation.high is not None:
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
    return observation.domain.lower().lstrip("www.")


def resolve_observations(context: JobContext, observations: Iterable[EvidenceObservation], *, now: int) -> Optional[CompensationResolution]:
    compatible = [row for row in observations if _compatible(context, row)]

    employer_ranges = [
        row for row in compatible
        if row.source_kind == "employer" and row.low is not None and row.high is not None and row.period == context.period
    ]
    if len(employer_ranges) == 1:
        row = employer_ranges[0]
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
