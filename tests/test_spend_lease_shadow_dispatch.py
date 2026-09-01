from __future__ import annotations

import logging
import threading

import pytest

from trusted_router.services.spend_lease_shadow_dispatch import (
    SpendLeaseShadowDispatcher,
)


def test_submit_never_waits_for_delivery() -> None:
    started = threading.Event()
    release = threading.Event()
    delivered: list[str] = []

    def deliver(event_id: str, _payload: dict[str, object]) -> None:
        started.set()
        assert release.wait(1)
        delivered.append(event_id)

    dispatcher = SpendLeaseShadowDispatcher(deliver)
    dispatcher.submit("event-1", {"value": 1})

    assert started.wait(1)
    assert delivered == []
    release.set()
    assert dispatcher.wait_for_idle(1)
    assert delivered == ["event-1"]
    dispatcher.close()


def test_delivery_retries_then_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def deliver(_event_id: str, _payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary telemetry outage")

    dispatcher = SpendLeaseShadowDispatcher(deliver, retry_seconds=0.001)
    with caplog.at_level(
        logging.WARNING,
        logger="trusted_router.services.spend_lease_shadow_dispatch",
    ):
        dispatcher.submit("event-1", {})

        assert dispatcher.wait_for_idle(1)
    assert attempts == 2
    assert dispatcher.stats().failures == 1
    assert dispatcher.stats().delivered == 1
    failure_records = [
        record
        for record in caplog.records
        if record.getMessage() == "spend_lease_shadow_delivery_failed"
    ]
    assert len(failure_records) == 1
    assert failure_records[0].levelno == logging.WARNING
    assert failure_records[0].exc_info is None
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    dispatcher.close()


def test_full_queue_drops_oldest_pending_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    delivered: list[str] = []

    def deliver(event_id: str, _payload: dict[str, object]) -> None:
        if event_id == "event-1":
            first_started.set()
            assert release_first.wait(1)
        delivered.append(event_id)

    dispatcher = SpendLeaseShadowDispatcher(deliver, max_pending=2)
    with caplog.at_level(
        logging.ERROR,
        logger="trusted_router.services.spend_lease_shadow_dispatch",
    ):
        dispatcher.submit("event-1", {})
        assert first_started.wait(1)
        dispatcher.submit("event-2", {})
        dispatcher.submit("event-3", {})
        dispatcher.submit("event-4", {})
    release_first.set()

    assert dispatcher.wait_for_idle(1)
    assert delivered == ["event-1", "event-3", "event-4"]
    assert dispatcher.stats().dropped == 1
    assert "spend_lease_shadow_queue_overflow" in caplog.text
    dispatcher.close()


def test_failure_log_never_contains_payload(caplog: pytest.LogCaptureFixture) -> None:
    failed = threading.Event()
    private_marker = "private-payload-marker"

    def deliver(_event_id: str, _payload: dict[str, object]) -> None:
        failed.set()
        raise RuntimeError("storage unavailable")

    dispatcher = SpendLeaseShadowDispatcher(deliver, retry_seconds=1)
    with caplog.at_level(
        logging.WARNING,
        logger="trusted_router.services.spend_lease_shadow_dispatch",
    ):
        dispatcher.submit("event-1", {"private": private_marker})
        assert failed.wait(1)
        dispatcher.close()

    assert "spend_lease_shadow_delivery_failed" in caplog.text
    assert private_marker not in caplog.text


def test_sustained_delivery_failure_escalates_once_threshold_is_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    threshold_reached = threading.Event()

    def deliver(_event_id: str, _payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 8:
            threshold_reached.set()
        raise TimeoutError("persistent telemetry outage")

    dispatcher = SpendLeaseShadowDispatcher(deliver, retry_seconds=0.001)
    with caplog.at_level(
        logging.WARNING,
        logger="trusted_router.services.spend_lease_shadow_dispatch",
    ):
        dispatcher.submit("event-1", {})
        assert threshold_reached.wait(1)
        dispatcher.close()

    records = {record.getMessage(): record for record in caplog.records}
    assert records["spend_lease_shadow_delivery_failed"].levelno == logging.WARNING
    assert records["spend_lease_shadow_delivery_still_failing"].levelno == logging.WARNING
    persistent = records["spend_lease_shadow_delivery_persistently_failing"]
    assert persistent.levelno == logging.ERROR
    assert persistent.consecutive_failures == 8
    assert persistent.exc_info is not None


@pytest.mark.parametrize(
    ("max_pending", "retry_seconds"),
    [(0, 1.0), (1, 0.0)],
)
def test_invalid_dispatcher_limits_fail_closed(
    max_pending: int,
    retry_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        SpendLeaseShadowDispatcher(
            lambda _event_id, _payload: None,
            max_pending=max_pending,
            retry_seconds=retry_seconds,
        )
