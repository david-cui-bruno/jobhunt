import sqlite3

from submission.ashby_policy import (
    can_attempt,
    ensure_lane_state,
    load_state,
    record_result,
    set_enabled,
)


def policy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_lane_state(conn)
    return conn


def test_ashby_starts_disabled() -> None:
    state = load_state(policy_db())
    assert state.enabled is False
    assert state.tier == 0
    assert state.consecutive_confirmed == 0
    assert can_attempt(state, now=1_000) is False


def test_three_confirmations_advance_from_180_to_90_minutes() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=1_000)
    assert state.next_attempt_at == 1_000 + 180 * 60
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    assert state.tier == 1
    assert state.consecutive_confirmed == 3
    assert state.next_attempt_at == 1_000 + 180 * 60 * 2 + 90 * 60


def test_ten_confirmations_advance_from_90_to_45_minutes() -> None:
    conn = policy_db()
    state = set_enabled(conn, True, now=1_000)
    for _ in range(10):
        state = record_result(conn, outcome="submitted", reason="confirmed", now=max(1_000, state.next_attempt_at))
    assert state.tier == 2
    assert state.consecutive_confirmed == 10
    assert state.next_attempt_at == 1_000 + 180 * 60 * 2 + 90 * 60 * 7 + 45 * 60


def test_spam_rejection_blocks_24_hours_and_slows_one_tier() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    for now in (1_000, 11_800, 22_600):
        record_result(conn, outcome="submitted", reason="confirmed", now=now)
    state = record_result(conn, outcome="manual", reason="flagged as possible spam", now=30_000)
    assert state.blocked_until == 30_000 + 24 * 60 * 60
    assert state.tier == 0
    assert state.consecutive_confirmed == 0
    assert can_attempt(state, now=state.blocked_until - 1) is False


def test_unconfirmed_click_disables_lane_for_uncertainty_pause() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    state = record_result(conn, outcome="manual", reason="clicked but confirmation uncertain", now=1_000)
    assert state.enabled is False
    assert state.last_outcome == "manual"
    assert can_attempt(state, now=1_001) is False


def test_needs_answers_keeps_tier_and_requires_new_posting_without_pause() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    for now in (1_000, 11_800, 22_600):
        state = record_result(conn, outcome="submitted", reason="confirmed", now=now)
    assert state.tier == 1
    state = record_result(conn, outcome="manual", reason="needs answers before pre-click", now=30_000)
    assert state.enabled is True
    assert state.tier == 1
    assert state.consecutive_confirmed == 3
    assert state.next_attempt_at == 30_000
    assert can_attempt(state, now=30_000) is True
