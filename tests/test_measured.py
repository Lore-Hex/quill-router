from __future__ import annotations

import logging

from trusted_router import measured


def test_measured_snapshot_returns_empty_data_when_analytics_is_unavailable(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(measured, "_CACHE", None)

    def fail_read(_name):
        raise TimeoutError("analytics unavailable")

    monkeypatch.setattr(
        measured.STORE,
        "public_analytics_snapshot",
        fail_read,
        raising=False,
    )

    with caplog.at_level(logging.WARNING, logger="trusted_router.measured"):
        snapshot = measured.measured_snapshot()

    assert snapshot["models"] == []
    assert snapshot["providers"] == []
    assert snapshot["total_samples"] == 0
    assert snapshot["generated_at"].endswith("Z")
    assert "serving empty snapshot (TimeoutError)" in caplog.text
    assert "analytics unavailable" not in caplog.text


def test_measured_snapshot_uses_stale_data_when_refresh_fails(
    monkeypatch,
) -> None:
    stale = {
        "models": [{"model": "minimax/minimax-m3", "provider": "minimax"}],
        "providers": [{"provider": "minimax"}],
        "total_samples": 11,
        "generated_at": "2026-08-10T18:00:00Z",
    }
    monkeypatch.setattr(measured, "_CACHE", (1.0, stale))
    monkeypatch.setattr(measured.time, "monotonic", lambda: 1.0 + measured._TTL_SECONDS + 1)

    def fail_read(_name):
        raise ConnectionError("analytics unavailable")

    monkeypatch.setattr(
        measured.STORE,
        "public_analytics_snapshot",
        fail_read,
        raising=False,
    )

    assert measured.measured_snapshot() is stale


def test_measured_snapshot_caches_a_failed_refresh(
    monkeypatch,
) -> None:
    calls = 0
    now = 100.0
    monkeypatch.setattr(measured, "_CACHE", None)
    monkeypatch.setattr(measured.time, "monotonic", lambda: now)

    def fail_read(_name):
        nonlocal calls
        calls += 1
        raise TimeoutError

    monkeypatch.setattr(
        measured.STORE,
        "public_analytics_snapshot",
        fail_read,
        raising=False,
    )

    first = measured.measured_snapshot()
    second = measured.measured_snapshot()

    assert second is first
    assert calls == 1


def test_measured_snapshot_uses_precomputed_data_without_raw_sample_scan(
    monkeypatch,
) -> None:
    snapshot = {
        "models": [],
        "providers": [],
        "total_samples": 17,
        "generated_at": "2026-08-10T19:00:00Z",
    }
    monkeypatch.setattr(measured, "_CACHE", None)
    monkeypatch.setattr(
        measured,
        "current_public_analytics_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        measured.STORE,
        "public_analytics_snapshot",
        lambda _name: snapshot,
        raising=False,
    )

    def fail_raw_scan(**_kwargs):
        raise AssertionError("raw benchmark scan should not run in production")

    monkeypatch.setattr(measured, "public_benchmark_samples", fail_raw_scan)

    assert measured.measured_snapshot() is snapshot
