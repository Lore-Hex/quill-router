from concurrent.futures import ThreadPoolExecutor

import pytest
import sentry_sdk

from trusted_router.routes.internal.gateway import _report_route_fallbacks
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.sentry_config import (
    before_send,
    capture_recovered_failover_warning,
    reset_sentry_floodgate_for_tests,
)


@pytest.fixture
def events(monkeypatch):
    recorded = []
    reset_sentry_floodgate_for_tests()

    def capture(event):
        scrubbed = before_send(event)
        if scrubbed is not None:
            recorded.append(scrubbed)

    monkeypatch.setattr(sentry_sdk, "capture_event", capture)
    yield recorded
    reset_sentry_floodgate_for_tests()


def event(model="openai/gpt-4.1-mini"):
    return {"level": "warning", "message": "recovered",
            "fingerprint": ["recovered-provider-failover", model, "atlas-cloud"]}


def test_repeated_recoveries_leave_room_for_real_errors(events):
    for _ in range(1000):
        capture_recovered_failover_warning(event())
    assert len(events) == 1
    assert before_send({"level": "error", "message": "settlement durability failed"}) is not None


def test_new_routes_remain_visible_with_a_bounded_warning_budget(events):
    for i in range(100):
        capture_recovered_failover_warning(event(f"model-{i}"))
    assert len(events) == 10
    assert before_send({"level": "error", "message": "unexpected billing failure"}) is not None


def test_recovery_warning_renews_after_six_hours(events):
    now = [0.]
    reset_sentry_floodgate_for_tests(clock=lambda: now[0])
    assert capture_recovered_failover_warning(event())
    now[0] = 6 * 3600 - 1
    assert not capture_recovered_failover_warning(event())
    now[0] += 1
    assert capture_recovered_failover_warning(event())
    assert len(events) == 2


def test_concurrent_recoveries_emit_one_warning(events):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: capture_recovered_failover_warning(event()), range(100)))
    assert len(events) == 1


def test_route_reports_keep_diagnostics_and_full_content_free_log_count(events, caplog):
    body = GatewaySettleRequest.model_construct(
        model="openai/gpt-4.1-mini", selected_model="openai/gpt-4.1-mini",
        endpoint="openai/gpt-4.1-mini@atlas-cloud/prepaid",
        selected_endpoint="openai/gpt-4.1-mini@openai/prepaid",
        route_fallbacks=1, route_failures=["atlas-cloud|route|UnconfiguredHostError"],
    )
    with caplog.at_level("INFO", logger="trusted_router.routes.internal.gateway"):
        for _ in range(10):
            _report_route_fallbacks(body)
    assert len(events) == 1
    assert events[0]["contexts"]["route_failover"]["failures"] == body.route_failures
    logs = [row for row in caplog.records if row.message == "gateway.provider_failover"]
    assert len(logs) == 10
    assert all(row.failed_providers == "atlas-cloud" for row in logs)


def test_telemetry_failure_cannot_break_settlement(monkeypatch):
    def broken(_):
        raise RuntimeError("telemetry unavailable")
    monkeypatch.setattr("trusted_router.sentry_config.capture_recovered_failover_warning", broken)
    _report_route_fallbacks(GatewaySettleRequest.model_construct(
        model="test/model", selected_model=None, endpoint="test", selected_endpoint=None,
        route_fallbacks=1, route_failures=["test|route|error"],
    ))
