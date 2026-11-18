"""A recovered provider failover is routing working as designed.

It used to be a Sentry warning fingerprinted per model/provider pair, which
opened dozens of "provider failover: <model> routed around <providers>"
issues. It is now a structured log line only; Sentry keeps its whole budget
for real faults.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
import sentry_sdk

from trusted_router.routes.internal.gateway import _report_route_fallbacks
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.sentry_config import before_send, reset_sentry_floodgate_for_tests

LOGGER = "trusted_router.routes.internal.gateway"


@pytest.fixture
def sentry_events(monkeypatch):
    recorded = []
    reset_sentry_floodgate_for_tests()
    monkeypatch.setattr(sentry_sdk, "capture_event", lambda e, *a, **k: recorded.append(e))
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda m, *a, **k: recorded.append(m))
    yield recorded
    reset_sentry_floodgate_for_tests()


def body(model="openai/gpt-4.1-mini", failures=("atlas-cloud|route|UnconfiguredHostError",)):
    return GatewaySettleRequest.model_construct(
        model=model, selected_model=model,
        endpoint=f"{model}@atlas-cloud/prepaid",
        selected_endpoint=f"{model}@openai/prepaid",
        route_fallbacks=len(failures), route_failures=list(failures),
    )


def failover_logs(caplog):
    return [row for row in caplog.records if row.message == "gateway.provider_failover"]


def test_failover_is_logged_every_time_and_never_reaches_sentry(sentry_events, caplog):
    with caplog.at_level("INFO", logger=LOGGER):
        for _ in range(10):
            _report_route_fallbacks(body())
    assert sentry_events == []
    logs = failover_logs(caplog)
    assert len(logs) == 10
    assert all(row.levelname == "INFO" for row in logs)
    assert all(row.failed_providers == "atlas-cloud" for row in logs)
    assert all(row.failover_model == "openai/gpt-4.1-mini" for row in logs)
    assert all(row.served_by == "openai/gpt-4.1-mini@openai/prepaid" for row in logs)
    assert all(row.fallbacks == 1 for row in logs)


def test_failover_log_keeps_raw_upstream_errors_out(sentry_events, caplog):
    with caplog.at_level("INFO", logger=LOGGER):
        _report_route_fallbacks(body(failures=("atlas-cloud|route|private upstream detail",)))
    [row] = failover_logs(caplog)
    assert "private upstream detail" not in str(row.__dict__)
    assert sentry_events == []


def test_many_distinct_routes_leave_the_sentry_budget_for_real_errors(sentry_events, caplog):
    with caplog.at_level("INFO", logger=LOGGER):
        for i in range(100):
            _report_route_fallbacks(body(model=f"vendor/model-{i}"))
    assert len(failover_logs(caplog)) == 100
    assert sentry_events == []
    # The full fifty-event hourly budget is still there for genuine errors.
    for i in range(50):
        assert before_send({"level": "error", "fingerprint": ["settle-failed", str(i)]}) is not None


def test_concurrent_failovers_are_each_logged(sentry_events, caplog):
    with caplog.at_level("INFO", logger=LOGGER):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: _report_route_fallbacks(body()), range(100)))
    assert len(failover_logs(caplog)) == 100
    assert sentry_events == []


def test_genuine_errors_still_reach_sentry(sentry_events):
    assert before_send({"level": "error", "message": "settlement durability failed"}) is not None


def test_telemetry_failure_cannot_break_settlement(sentry_events, caplog):
    broken = GatewaySettleRequest.model_construct(
        model="test/model", selected_model=None, endpoint="test", selected_endpoint=None,
        route_fallbacks=1, route_failures=["test|route|error"],
    )

    class Boom:
        def __bool__(self):
            raise RuntimeError("telemetry unavailable")

    broken.selected_model = Boom()
    with caplog.at_level("INFO", logger=LOGGER):
        _report_route_fallbacks(broken)
    assert "route fallback log failed" in caplog.text
    assert sentry_events == []


def test_settle_with_a_failover_logs_it_and_sends_nothing_to_sentry(sentry_events, caplog):
    """Functional: the real authorize -> settle HTTP path after a failover."""
    from tests.test_gateway_cache_billing import _authorize, _client_and_key

    client, key = _client_and_key()
    auth = _authorize(client, key, "anthropic/claude-haiku-4.5")
    with caplog.at_level("INFO", logger=LOGGER):
        settle = client.post(
            "/v1/internal/gateway/settle",
            json={
                "authorization_id": auth["authorization_id"],
                "actual_input_tokens": 500,
                "actual_output_tokens": 100,
                "request_id": "gw-failover-logged",
                "elapsed_seconds": 1.0,
                "route_fallbacks": 1,
                "route_failures": ["atlas-cloud|route|UnconfiguredHostError"],
            },
        )
    assert settle.status_code == 200, settle.text
    [row] = failover_logs(caplog)
    assert row.failed_providers == "atlas-cloud"
    assert row.fallbacks == 1
    assert sentry_events == []
