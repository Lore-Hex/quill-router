from __future__ import annotations

import datetime as dt

from trusted_router.public_analytics_snapshots import current_public_analytics_snapshot


def test_current_public_analytics_snapshot_accepts_fresh_utc_payload() -> None:
    payload = {"generated_at": "2026-08-10T19:00:00Z", "total_samples": 7}

    assert (
        current_public_analytics_snapshot(
            "leaderboard",
            reader=lambda _name: payload,
            now=dt.datetime(2026, 8, 10, 19, 9, tzinfo=dt.UTC),
        )
        is payload
    )


def test_current_public_analytics_snapshot_rejects_stale_future_and_malformed_payloads() -> None:
    now = dt.datetime(2026, 8, 10, 19, 20, tzinfo=dt.UTC)
    invalid = [
        {"generated_at": "2026-08-10T19:00:00Z"},
        {"generated_at": "2026-08-10T19:21:00Z"},
        {"generated_at": "not-a-date"},
        {"total_samples": 1},
    ]

    for payload in invalid:
        assert (
            current_public_analytics_snapshot(
                "leaderboard",
                reader=lambda _name, value=payload: value,
                now=now,
            )
            is None
        )
