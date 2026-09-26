from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.routes.internal import gateway
from trusted_router.routing_state import RoutingState
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest


def test_gateway_settlement_affinity_performance_and_hard_filters(monkeypatch):
    state = RoutingState()
    monkeypatch.setattr(gateway, "ROUTING_STATE", state)
    client, key = _client_and_key()
    body = {
        "api_key_hash": key["hash"],
        "model": "google/gemma-4-31b-it",
        "route_type": "chat.completions",
        "region": "us-central1",
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
        "cache_affinity_key": "a" * 64,
        "cache_affinity_explicit": True,
    }

    def authorize(**overrides):
        response = client.post("/v1/internal/gateway/authorize", json={**body, **overrides})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    original = authorize()
    alternate = next(
        route for route in original["route_candidates"] if route["provider"] != original["provider"]
    )
    response = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": original["authorization_id"],
            "selected_endpoint": alternate["endpoint_id"],
            "actual_input_tokens": 10,
            "actual_output_tokens": 10,
            "route_type": "chat.completions",
            "streamed": True,
            "first_token_seconds": 0.5,
            "elapsed_seconds": 1,
            "cache_affinity_key": "a" * 64,
            "cache_affinity_explicit": True,
        },
    )
    assert response.status_code == 200, response.text
    assert state.session_count == 1
    assert authorize()["endpoint_id"] == alternate["endpoint_id"]
    assert authorize(cache_affinity_key="b" * 64)["endpoint_id"] == original["endpoint_id"]
    assert authorize(provider={"only": [original["provider"]]})["provider"] == original["provider"]
    assert authorize(provider={"order": [original["provider"]]})["provider"] == original["provider"]
    assert (
        authorize(cache_affinity_key=None, provider={"preferred_max_latency": 1})["endpoint_id"]
        == alternate["endpoint_id"]
    )


def test_routing_hints_are_excluded_from_durable_payloads():
    for schema, required in (
        (GatewayAuthorizeRequest, {"model": "test/model", "api_key_hash": "key"}),
        (GatewaySettleRequest, {"authorization_id": "authorization"}),
    ):
        body = schema(**required, cache_affinity_key="a" * 64, cache_affinity_explicit=True)
        assert "cache_affinity_key" not in body.model_dump()
        assert "cache_affinity_explicit" not in body.model_dump()


def test_failed_request_does_not_seed_affinity(monkeypatch):
    state = RoutingState()
    monkeypatch.setattr(gateway, "ROUTING_STATE", state)
    client, key = _client_and_key()
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "google/gemma-4-31b-it",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
            "cache_affinity_key": "a" * 64,
            "cache_affinity_explicit": True,
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": response.json()["data"]["authorization_id"],
            "error_status": 503,
            "error_type": "provider_error",
        },
    )
    assert response.status_code == 200, response.text
    assert state.session_count == 0
