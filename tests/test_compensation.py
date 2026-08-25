import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path

from compensation.models import CompensationResolution, EvidenceObservation, JobContext
from compensation.normalize import extract_usd_observations, requested_period
from compensation.resolve import resolve_observations
from compensation.schema import ensure_compensation_schema, load_resolution, store_resolution
from submission.database import connect_tracker


def context() -> JobContext:
    return JobContext(
        posting_id="p1",
        company="Acme",
        title="Software Engineer Intern",
        location="New York, NY",
        employment_type="intern",
        currency="USD",
        period="hour",
    )


def observation() -> EvidenceObservation:
    return EvidenceObservation(
        url="https://levels.fyi/acme",
        domain="levels.fyi",
        title="Acme intern pay",
        low=Decimal("40"),
        high=Decimal("50"),
        point=None,
        currency="USD",
        period="hour",
        source_kind="market",
        observed_at=1_787_600_000,
    )


def resolution() -> CompensationResolution:
    return CompensationResolution(
        context=context(),
        amount=Decimal("45"),
        method="market_median",
        evidence=(observation(),),
        researched_at=1_787_600_000,
        expires_at=1_790_192_000,
    )


def test_schema_round_trip_requires_exact_fresh_context(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "tracker.db")
    conn.row_factory = sqlite3.Row
    ensure_compensation_schema(conn)
    store_resolution(conn, resolution())
    assert load_resolution(conn, context(), now=1_787_600_001) == resolution()
    wrong = dataclasses.replace(context(), company="Other")
    assert load_resolution(conn, wrong, now=1_787_600_001) is None
    assert load_resolution(conn, context(), now=1_790_192_001) is None


def test_resolution_reconstructs_decimal_and_optional_evidence_values(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "tracker.db")
    conn.row_factory = sqlite3.Row
    ensure_compensation_schema(conn)
    expected = CompensationResolution(
        context=context(),
        amount=Decimal("42.50"),
        method="single_source",
        evidence=(
            dataclasses.replace(observation(), low=None, high=None, point=Decimal("42.50")),
        ),
        researched_at=1_787_600_000,
        expires_at=1_790_192_000,
    )

    store_resolution(conn, expected)

    actual = load_resolution(conn, context(), now=1_787_600_001)
    assert actual == expected
    assert isinstance(actual.amount, Decimal)
    assert isinstance(actual.evidence[0].point, Decimal)
    assert actual.evidence[0].low is None
    assert actual.evidence[0].high is None


def test_connect_tracker_initializes_compensation_schema(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    try:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(compensation_evidence)").fetchall()
        }
        assert columns == {
            "cache_key": "TEXT",
            "posting_id": "TEXT",
            "company": "TEXT",
            "normalized_role": "TEXT",
            "location": "TEXT",
            "employment_type": "TEXT",
            "currency": "TEXT",
            "period": "TEXT",
            "resolved_amount": "TEXT",
            "method": "TEXT",
            "evidence_json": "TEXT",
            "researched_at": "INTEGER",
            "expires_at": "INTEGER",
        }
    finally:
        conn.close()


def test_ensure_compensation_schema_preserves_caller_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller_owned (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO caller_owned (id) VALUES (1)")

    ensure_compensation_schema(conn)

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_owned").fetchone() == (0,)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compensation_evidence'"
    ).fetchone() is None


def employer(low: str, high: str, *, domain: str = "acme.com", period: str = "hour", currency: str = "USD", observed_at: int = 90) -> EvidenceObservation:
    return EvidenceObservation(
        url=f"https://{domain}/jobs/1",
        domain=domain,
        title="Acme Software Engineer Intern compensation",
        low=Decimal(low),
        high=Decimal(high),
        point=None,
        currency=currency,
        period=period,
        source_kind="employer",
        observed_at=observed_at,
    )


def market(low: str, high: str, *, domain: str = "levels.fyi", period: str = "hour", currency: str = "USD", observed_at: int = 90) -> EvidenceObservation:
    return EvidenceObservation(
        url=f"https://{domain}/acme-intern-pay",
        domain=domain,
        title="Acme Software Engineer Intern New York pay",
        low=Decimal(low),
        high=Decimal(high),
        point=None,
        currency=currency,
        period=period,
        source_kind="market",
        observed_at=observed_at,
    )


def market_point(point: str, *, domain: str = "levels.fyi", period: str = "hour", currency: str = "USD", observed_at: int = 90) -> EvidenceObservation:
    return EvidenceObservation(
        url=f"https://{domain}/acme-intern-pay",
        domain=domain,
        title="Acme Software Engineer Intern New York pay",
        low=None,
        high=None,
        point=Decimal(point),
        currency=currency,
        period=period,
        source_kind="market",
        observed_at=observed_at,
    )


import pytest


@pytest.mark.parametrize(("text", "expected"), [
    ("$40-$50 per hour", ("40", "50", "hour")),
    ("USD 90,000 to 110,000 per year", ("90000", "110000", "year")),
    ("Pay is competitive", None),
    ("€50,000 per year", None),
    ("$50,000", None),
    ("Compensation is USD $42.50 – USD $55.25 hourly", ("42.50", "55.25", "hour")),
    ("Range is 40 to 50 per hour", None),
    ("$50-$40 per hour", None),
    ("$4-$6 per hour", None),
    ("$1,200,000-$1,300,000 annual", None),
])
def test_extract_usd_ranges_is_conservative(text, expected):
    rows = extract_usd_observations(
        text, url="https://levels.fyi/x", title="x", source_kind="market", observed_at=100,
    )
    if expected is None:
        assert rows == []
    else:
        assert (str(rows[0].low), str(rows[0].high), rows[0].period) == expected
        assert rows[0].currency == "USD"
        assert rows[0].domain == "levels.fyi"


@pytest.mark.parametrize(("question", "expected"), [
    ("What is your desired hourly rate?", "hour"),
    ("Requested rate", "hour"),
    ("What annual base salary do you expect?", "year"),
    ("Desired yearly compensation", "year"),
    ("Desired compensation", None),
])
def test_requested_period_classifies_explicit_periods(question, expected):
    assert requested_period(question) == expected


def test_exact_employer_range_midpoint_wins():
    result = resolve_observations(context(), [employer("42", "58"), market("40", "50")], now=100)
    assert result.amount == Decimal("50")
    assert result.method == "employer_midpoint"
    assert result.expires_at == 100 + 30 * 86400


def test_multiple_employer_ranges_fail_closed():
    result = resolve_observations(
        context(),
        [employer("42", "58"), employer("43", "59"), market("40", "50"), market("44", "54", domain="indeed.com")],
        now=100,
    )
    assert result is None


def test_two_independent_domains_resolve_market_median():
    rows = [market("40", "50", domain="levels.fyi"), market("44", "54", domain="indeed.com")]
    result = resolve_observations(context(), rows, now=100)
    assert result.amount == Decimal("47")
    assert result.method == "market_median"


@pytest.mark.parametrize("rows", [
    [market("40", "50", domain="levels.fyi")],
    [market("40", "50", domain="levels.fyi"), market("45", "55", domain="levels.fyi")],
    [market("20", "25"), market("70", "80", domain="indeed.com")],
    [market("40", "50", currency="EUR"), market("44", "54", domain="indeed.com")],
])
def test_insufficient_or_inconsistent_market_evidence_fails_closed(rows):
    assert resolve_observations(context(), rows, now=100) is None


def test_converts_annual_to_hourly_only_with_explicit_periods():
    rows = [market("83200", "104000", domain="levels.fyi", period="year"), market("44", "54", domain="indeed.com")]
    result = resolve_observations(context(), rows, now=100)
    assert result.amount == Decimal("47")


def test_converts_hourly_to_annual_and_rounds_to_nearest_thousand():
    annual_context = dataclasses.replace(context(), period="year")
    rows = [market("40", "50", domain="levels.fyi"), market("44", "54", domain="indeed.com")]
    result = resolve_observations(annual_context, rows, now=100)
    assert result.amount == Decimal("98000")


def test_rejects_context_incompatible_observations_before_arithmetic():
    rows = [
        market("40", "50", domain="levels.fyi"),
        dataclasses.replace(market("44", "54", domain="indeed.com"), title="OtherCo senior engineer San Francisco"),
    ]
    assert resolve_observations(context(), rows, now=100) is None


def test_uses_market_points_and_rejects_wide_spread_after_dedupe():
    ok = [market_point("45", domain="levels.fyi"), market_point("49", domain="indeed.com")]
    assert resolve_observations(context(), ok, now=100).amount == Decimal("47")

    wide = [market_point("25", domain="levels.fyi"), market_point("51", domain="indeed.com")]
    assert resolve_observations(context(), wide, now=100) is None


def test_extract_rejects_foreign_and_mixed_currency_markers():
    for text in (
        "€50-$60 per hour",
        "CAD $50-$60 per hour",
        "A$50-$60 per hour",
        "C$50-$60 per hour",
        "AUD $90,000-$110,000 annual",
    ):
        assert extract_usd_observations(
            text,
            url="https://levels.fyi/x",
            title="x",
            source_kind="market",
            observed_at=100,
        ) == []


def test_extract_preserves_non_www_domain_prefixes():
    rows = extract_usd_observations(
        "$40-$50 per hour",
        url="https://wwww.levels.fyi/x",
        title="x",
        source_kind="market",
        observed_at=100,
    )
    assert rows[0].domain == "wwww.levels.fyi"


def test_extract_rejects_unspaced_thousands_and_ambiguous_questions_stay_unknown():
    assert extract_usd_observations(
        "USD 90000 to 110000 per year",
        url="https://levels.fyi/x",
        title="x",
        source_kind="market",
        observed_at=100,
    ) == []
    assert requested_period("What are your compensation expectations?") is None


@pytest.mark.parametrize("row", [
    employer("58", "42"),
    employer("0", "42"),
    employer("4", "6"),
    employer("501", "600"),
])
def test_invalid_employer_ranges_fail_closed_before_midpoint(row):
    assert resolve_observations(context(), [row], now=100) is None


def test_multiple_compatible_employer_ranges_fail_closed():
    rows = [
        employer("42", "58"),
        employer("43", "59"),
        market("40", "50"),
        market("44", "54", domain="indeed.com"),
    ]
    assert resolve_observations(context(), rows, now=100) is None


def test_company_matching_is_token_boundary_based():
    meta_context = dataclasses.replace(context(), company="Meta")
    rows = [
        dataclasses.replace(market("40", "50", domain="metadata.com"), title="Metadata intern New York pay"),
        market("44", "54", domain="indeed.com"),
    ]
    assert resolve_observations(meta_context, rows, now=100) is None


def test_role_and_location_compatibility_avoid_substring_false_positives():
    rows = [
        dataclasses.replace(market("40", "50", domain="levels.fyi"), title="Acme winter pay New York"),
        dataclasses.replace(market("44", "54", domain="indeed.com"), title="Acme Software Engineer Intern Newark pay"),
    ]
    assert resolve_observations(context(), rows, now=100) is None


@pytest.mark.parametrize("text", [
    "£40-$50 per hour",
    "MXN $40-$50 per hour",
    "NZ$40-$50 per hour",
    "HK$40-$50 per hour",
    "¥40-$50 per hour",
    "JPY 40 to 50 per hour",
    "GBP $90,000-$110,000 annual",
    "SGD40 to 50 per hour",
    "NYC $40-$50 per hour",
])
def test_extract_rejects_non_usd_currency_qualifiers_without_denylist(text):
    assert extract_usd_observations(
        text,
        url="https://levels.fyi/x",
        title="x",
        source_kind="market",
        observed_at=100,
    ) == []


@pytest.mark.parametrize(("text", "expected"), [
    ("$40-$50 per hour", ("40", "50", "hour")),
    ("USD 40 to 50 per hour", ("40", "50", "hour")),
    ("USD $90,000-$110,000 annual", ("90000", "110000", "year")),
])
def test_extract_accepts_only_literal_usd_or_unqualified_dollar(text, expected):
    rows = extract_usd_observations(
        text,
        url="https://levels.fyi/x",
        title="x",
        source_kind="market",
        observed_at=100,
    )
    assert (str(rows[0].low), str(rows[0].high), rows[0].period) == expected


@pytest.mark.parametrize("text", [
    "$40-$50 per hour CAD",
    "$40-$50 per hour aud",
    "$40-$50 per hour nzd",
    "$40-$50 per hour hkd",
    "$40-$50 per hour mxn",
    "$40-$50 per hour gbp",
    "$40-$50 per hour eur",
    "$40-$50 per hour jpy",
    "$40-$50 per hour cny",
    "$40-$50 per hour inr",
    "$40-$50 per hour chf",
    "Comp listed in Canadian dollars $40-$50 per hour",
    "Comp listed in euros $40-$50 per hour",
    "Comp listed in pounds $40-$50 per hour",
    "Comp listed in yen $40-$50 per hour",
    "Comp listed in rupees $40-$50 per hour",
    "Comp listed as AU dollars $40-$50 per hour",
    "$40-$50 per hour €",
    "$40-$50 per hour ¥",
    "$40-$50 per hour £",
])
def test_extract_rejects_currency_suffix_lowercase_spelled_and_distant_qualifiers(text):
    assert extract_usd_observations(
        text,
        url="https://levels.fyi/x",
        title="x",
        source_kind="market",
        observed_at=100,
    ) == []
