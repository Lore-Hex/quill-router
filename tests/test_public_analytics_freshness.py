"""/status.json publishes this cloud's drain lag, on every cloud, always.

The signal existed before this and nothing called it. These tests pin the
wiring: the `analytics` key is present in every outcome, a read failure
publishes `available: false` rather than dropping the key or reusing the last
good number, and `_compact_status_json` carries it out to the wire.
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    BACKEND_MEMORY,
    BACKEND_POSTGRES,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    OutboxFreshness,
)
from trusted_router.routes import public as public_routes
from trusted_router.storage import STORE


def _snapshot(monkeypatch) -> dict[str, object]:
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    return public_routes._status_snapshot(Settings(environment="local"))


def test_status_snapshot_publishes_the_analytics_section(monkeypatch) -> None:
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness(
            backend=BACKEND_POSTGRES,
            oldest_enqueued_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=42),
        ),
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(section, dict)
    assert section["available"] is True
    assert section["backend"] == BACKEND_POSTGRES
    assert 40 <= float(section["drain_lag_seconds"]) < DEFAULT_MAX_DRAIN_LAG_SECONDS
    assert section["generated_at"].endswith("Z")


def test_a_drained_outbox_publishes_zero_lag_not_a_missing_key(monkeypatch) -> None:
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=None),
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(section, dict)
    assert section["available"] is True
    assert section["drain_lag_seconds"] == 0.0


def test_a_raising_store_publishes_unavailable_and_never_omits_the_key(monkeypatch) -> None:
    """Dropping the key on failure would read as "this cloud runs older code".

    The fleet checker has a separate, louder branch for that, and it tells the
    operator to redeploy. A database that cannot be read is a different problem
    with a different fix, so it gets a different published shape.
    """

    def boom() -> OutboxFreshness:
        raise RuntimeError("dsql: connection refused")

    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        boom,
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert section == {"available": False, "reason": REASON_UNREACHABLE}


def test_a_failed_read_never_republishes_the_last_good_number(monkeypatch) -> None:
    """A stale-but-plausible lag is indistinguishable from a healthy one."""
    readings = [
        OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=None),
        OutboxFreshness.unavailable(BACKEND_POSTGRES, REASON_UNREACHABLE),
    ]
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: readings.pop(0),
        raising=False,
    )

    healthy = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]
    broken = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(healthy, dict) and healthy["available"] is True
    assert broken == {"available": False, "reason": REASON_UNREACHABLE}


def test_the_in_memory_backend_says_not_configured_rather_than_zero() -> None:
    """No outbox and no drain must not publish the healthiest possible number."""
    reading = STORE.operational_analytics_outbox_freshness()

    assert reading.available is False
    assert reading.backend == BACKEND_MEMORY
    assert reading.reason == REASON_NOT_CONFIGURED


def test_status_json_carries_the_section_to_the_wire(
    client: TestClient,
    monkeypatch,
) -> None:
    """_compact_status_json strips tooltip data; it must not strip this."""
    section = {"available": True, "backend": "postgres", "drain_lag_seconds": 1.5}
    monkeypatch.setattr(
        public_routes,
        "_status_snapshot",
        lambda _settings: {"components": [], ANALYTICS_STATUS_KEY: section},
    )

    response = client.get("/status.json")

    assert response.status_code == 200
    assert response.json()["data"][ANALYTICS_STATUS_KEY] == section
