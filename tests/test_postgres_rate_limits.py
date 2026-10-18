"""PostgreSQL-specific bounded retention and backward-clock behavior."""

import datetime as dt

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn


def test_counter_reuses_row_and_does_not_reset_for_backward_clock():
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    start = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)

    def hit(offset):
        return store.hit_rate_limit(
            namespace="social_rewards_claim_v1", subject="user",
            limit=1, window_seconds=60, now=start + dt.timedelta(seconds=offset),
        )

    assert hit(0).allowed
    assert not hit(0).allowed
    assert hit(60).allowed
    older = hit(0)
    assert not older.allowed
    assert older.reset_at == "2026-10-17T12:02:00Z"
    assert not hit(60).allowed
    for minute in range(2, 10):
        assert hit(minute * 60).allowed
    row = conn.execute("SELECT count(*) FROM tr_entities WHERE kind = %s", ("rate_limit",)).fetchone()
    assert row[0] == 1
