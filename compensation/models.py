from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple


@dataclass(frozen=True)
class JobContext:
    posting_id: str
    company: str
    title: str
    location: str
    employment_type: str
    currency: str
    period: str


@dataclass(frozen=True)
class EvidenceObservation:
    url: str
    domain: str
    title: str
    low: Optional[Decimal]
    high: Optional[Decimal]
    point: Optional[Decimal]
    currency: str
    period: str
    source_kind: str
    observed_at: int


@dataclass(frozen=True)
class CompensationResolution:
    context: JobContext
    amount: Decimal
    method: str
    evidence: Tuple[EvidenceObservation, ...]
    researched_at: int
    expires_at: int
