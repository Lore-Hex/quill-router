from __future__ import annotations

import datetime as dt
import math

import pytest

import clickhouse.check_fleet_analytics_freshness as fleet_check
from trusted_router import operational_analytics_direct, storage_gcp
from trusted_router.operational_analytics_direct import DirectOperationalAnalyticsSink
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    BACKEND_DIRECT,
    BACKEND_SPANNER,
    DROPPED_TOTAL_FIELD,
    FLUSH_FAILURES_FIELD,
    SECONDS_SINCE_LAST_DELIVERY_FIELD,
    OutboxFreshness,
    analytics_status_from_reading,
)
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_postgres import PostgresStore


def _sink(*, buffer_rows: int = 4) -> DirectOperationalAnalyticsSink:
    return DirectOperationalAnalyticsSink(
        url="http://clickhouse.internal:8123",
        database="tr",
        user="tr",
        password="pw",  # noqa: S106
        buffer_rows=buffer_rows,
        post=lambda _url, _body, _headers: None,
        start_thread=False,
    )


def _enqueue(sink: DirectOperationalAnalyticsSink, event_id: str) -> None:
    # An intentionally incomplete synthetic row takes the quarantine path,
    # which is still a real ClickHouse delivery and keeps this test independent
    # of the much larger synthetic payload schema.
    sink._publish("synthetic", event_id, {"id": event_id})


def _gcp_reading(sink: DirectOperationalAnalyticsSink) -> OutboxFreshness:
    store = object.__new__(SpannerBigtableStore)
    store._operational_analytics_outbox = sink
    return store.operational_analytics_outbox_freshness()


def _payload(reading: OutboxFreshness, *, now: dt.datetime) -> dict[str, object]:
    return {
        ANALYTICS_STATUS_KEY: analytics_status_from_reading(reading, now=now),
    }


def test_healthy_direct_sink_with_recent_delivery_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime.now(dt.UTC)
    sink = _sink()
    monkeypatch.setattr(operational_analytics_direct.time, "time", now.timestamp)
    _enqueue(sink, "delivered")
    assert sink.flush() == 1
    _enqueue(sink, "newly-buffered")

    reading = _gcp_reading(sink)

    assert reading.backend == BACKEND_DIRECT
    assert reading.oldest_enqueued_at is not None
    assert reading.seconds_since_last_delivery == pytest.approx(0.0, abs=0.1)
    assert reading.dropped_total == 0
    assert reading.flush_failures == 0
    assert fleet_check.evaluate(
        _payload(reading, now=now),
        now=now,
        expected_backend=BACKEND_SPANNER,
    ) == []


def test_idle_but_healthy_direct_sink_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = dt.datetime.now(dt.UTC)
    sink = _sink()
    monkeypatch.setattr(operational_analytics_direct.time, "time", now.timestamp)
    _enqueue(sink, "delivered")
    assert sink.flush() == 1

    reading = _gcp_reading(sink)

    assert reading.oldest_enqueued_at is None
    assert reading.seconds_since_last_delivery == pytest.approx(0.0, abs=0.1)
    assert fleet_check.evaluate(_payload(reading, now=now), now=now) == []


def test_never_delivered_is_distinct_from_healthy_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime.now(dt.UTC)
    monkeypatch.setattr(storage_gcp.time, "time", now.timestamp)
    reading = _gcp_reading(_sink())

    assert reading.oldest_enqueued_at is None
    assert reading.seconds_since_last_delivery is None
    assert reading.dropped_total == 0
    assert reading.flush_failures == 0
    section = analytics_status_from_reading(reading, now=now)
    assert section[SECONDS_SINCE_LAST_DELIVERY_FIELD] is None
    assert fleet_check.evaluate({ANALYTICS_STATUS_KEY: section}, now=now) == []


def test_never_delivered_with_an_ageing_row_alarms() -> None:
    now = dt.datetime.now(dt.UTC)
    reading = OutboxFreshness(
        backend=BACKEND_DIRECT,
        oldest_enqueued_at=now - dt.timedelta(seconds=601),
        seconds_since_last_delivery=None,
        dropped_total=0,
        flush_failures=0,
    )

    problems = fleet_check.evaluate(_payload(reading, now=now), now=now)

    assert any("has never delivered" in problem for problem in problems)


def test_dead_bounded_sink_reproduces_incident_old_signal_passes_new_signal_alarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping oldest keeps backlog age young; delivery health catches it.

    This is the 2026-08-26 failure shape. The pre-change store mislabeled the
    sink as Spanner and published only the retained buffer head. Because that
    buffer is bounded, old rows disappear and the legacy reading passes. The
    direct reading must fail on delivery age and counted loss instead.
    """
    now = dt.datetime.now(dt.UTC)
    sink = _sink(buffer_rows=2)

    # Establish one real delivery, then make the sink stale for twenty minutes.
    monkeypatch.setattr(
        operational_analytics_direct.time,
        "time",
        lambda: now.timestamp() - 1_200,
    )
    _enqueue(sink, "last-success")
    assert sink.flush() == 1
    monkeypatch.setattr(operational_analytics_direct.time, "time", now.timestamp)

    def dead_post(_url: str, _body: bytes, _headers: dict[str, str]) -> None:
        raise OSError("ClickHouse refuses the credential")

    sink._post = dead_post
    for index in range(4):
        _enqueue(sink, f"lost-{index}")
    with pytest.raises(OSError):
        sink.flush()

    oldest = sink.oldest_enqueued_at()
    assert oldest is not None
    legacy = OutboxFreshness(
        backend=BACKEND_SPANNER,
        oldest_enqueued_at=oldest,
    )
    # The old status shape says healthy: only the two newest rows survive, so
    # its oldest retained timestamp is seconds old rather than 20 minutes old.
    assert fleet_check.evaluate(
        _payload(legacy, now=now),
        now=now,
        expected_backend=BACKEND_SPANNER,
    ) == []

    reading = _gcp_reading(sink)
    assert reading.seconds_since_last_delivery == pytest.approx(1_200.0, abs=0.1)
    assert reading.dropped_total == 2
    assert reading.flush_failures == 1
    problems = fleet_check.evaluate(
        _payload(reading, now=now),
        now=now,
        expected_backend=BACKEND_SPANNER,
    )

    assert any("has not delivered for 1200s" in problem for problem in problems)
    assert any("rows dropped=2" in problem for problem in problems)
    assert any("flush failures=1" in problem for problem in problems)
    assert not any("oldest undelivered" in problem for problem in problems)


def test_postgres_store_recognises_a_direct_sink_without_touching_its_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime.now(dt.UTC)
    monkeypatch.setattr(operational_analytics_direct.time, "time", now.timestamp)
    sink = _sink()
    _enqueue(sink, "delivered")
    assert sink.flush() == 1
    store = object.__new__(PostgresStore)
    store._operational_analytics_outbox = sink

    reading = store.operational_analytics_outbox_freshness()

    assert reading.backend == BACKEND_DIRECT
    assert reading.seconds_since_last_delivery == pytest.approx(0.0, abs=0.1)


def test_direct_publisher_narrows_backend_and_numeric_fields() -> None:
    now = dt.datetime.now(dt.UTC)
    section = analytics_status_from_reading(
        OutboxFreshness(
            backend=BACKEND_DIRECT,
            seconds_since_last_delivery=-3.5,
            dropped_total=-2,
            flush_failures=object(),  # type: ignore[arg-type]
        ),
        now=now,
    )

    assert section["backend"] == BACKEND_DIRECT
    assert section[SECONDS_SINCE_LAST_DELIVERY_FIELD] == 0.0
    assert section[DROPPED_TOTAL_FIELD] == 0
    assert section[FLUSH_FAILURES_FIELD] is None

    non_finite = analytics_status_from_reading(
        OutboxFreshness(
            backend=BACKEND_DIRECT,
            seconds_since_last_delivery=math.inf,
            dropped_total=0,
            flush_failures=0,
        ),
        now=now,
    )
    assert non_finite[SECONDS_SINCE_LAST_DELIVERY_FIELD] is None


def test_fleet_checker_keeps_accepting_spanner_and_alarms_on_stalled_direct() -> None:
    now = dt.datetime.now(dt.UTC)
    spanner = OutboxFreshness(backend=BACKEND_SPANNER)
    assert fleet_check.evaluate(
        _payload(spanner, now=now),
        now=now,
        expected_backend=BACKEND_SPANNER,
    ) == []

    stalled = OutboxFreshness(
        backend=BACKEND_DIRECT,
        seconds_since_last_delivery=601.0,
        dropped_total=0,
        flush_failures=0,
    )
    problems = fleet_check.evaluate(
        _payload(stalled, now=now),
        now=now,
        expected_backend=BACKEND_SPANNER,
    )
    assert any("has not delivered" in problem for problem in problems)
    assert not any("answered by backend" in problem for problem in problems)
