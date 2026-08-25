import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path

from compensation.models import CompensationResolution, EvidenceObservation, JobContext
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
