from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.providers import ProviderClient, ProviderError, ProviderResult
from trusted_router.storage import STORE


def test_stablecoin_checkout_uses_stripe_crypto_payment_method(monkeypatch) -> None:
    app = create_app(Settings(environment="test", stripe_secret_key="sk_test"))  # noqa: S106
    local_client = TestClient(app)
    captured: dict[str, object] = {}

    def create_session(**kwargs):
        captured.update(kwargs)
        return {"id": "cs_crypto", "url": "https://checkout.stripe.test/crypto"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session)

    checkout = local_client.post(
        "/v1/billing/checkout",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"amount": 25, "payment_method": "stablecoin"},
    )

    assert checkout.status_code == 201, checkout.text
    data = checkout.json()["data"]
    assert data["mode"] == "stripe_stablecoin"
    assert captured["payment_method_types"] == ["crypto"]
    assert captured["customer_email"] == "alice@example.com"
    assert captured["metadata"] == {"workspace_id": data["workspace_id"], "payment_method": "stablecoin"}


def test_trustedrouter_auto_rolls_over_to_next_provider(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    attempts: list[str] = []

    async def fake_chat(_self, model, _body):
        attempts.append(model.id)
        if model.id == "anthropic/claude-opus-4.7":
            raise ProviderError(model.provider, 503, "upstream unavailable")
        return ProviderResult(
            text="fallback ok",
            input_tokens=3,
            output_tokens=2,
            finish_reason="stop",
            provider_name="Anthropic",
            request_id="req_auto_fallback",
            usage_estimated=False,
        )

    monkeypatch.setattr(ProviderClient, "chat", fake_chat)

    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "trustedrouter/auto",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert attempts[:2] == ["anthropic/claude-opus-4.7", "anthropic/claude-3-5-sonnet"]
    assert payload["model"] == "anthropic/claude-3-5-sonnet"
    assert payload["trustedrouter"]["requested_model"] == "trustedrouter/auto"
    assert payload["trustedrouter"]["selected_model"] == "anthropic/claude-3-5-sonnet"
    generation = next(iter(STORE.generation_store.generations.values()))
    assert generation.model == "anthropic/claude-3-5-sonnet"


def test_models_array_rolls_over_and_provider_filters_apply(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    attempts: list[str] = []

    async def fake_chat(_self, model, _body):
        attempts.append(model.id)
        if model.id == "deepseek/deepseek-v4-flash":
            raise ProviderError(model.provider, 429, "busy")
        return ProviderResult(
            text="mistral ok",
            input_tokens=4,
            output_tokens=2,
            finish_reason="stop",
            provider_name="Mistral",
            request_id="req_models_fallback",
            usage_estimated=False,
        )

    monkeypatch.setattr(ProviderClient, "chat", fake_chat)

    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "deepseek/deepseek-v4-flash",
            "models": ["mistral/mistral-small-2603", "openai/gpt-4o-mini"],
            "provider": {"ignore": ["openai"]},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert attempts == ["deepseek/deepseek-v4-flash", "mistral/mistral-small-2603"]
    assert payload["model"] == "mistral/mistral-small-2603"
    assert payload["trustedrouter"]["rollover_failures"]


def test_provider_order_sort_and_no_fallbacks_shape_candidate_list(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    attempts: list[str] = []

    async def fake_chat(_self, model, _body):
        attempts.append(model.id)
        return ProviderResult(
            text="ok",
            input_tokens=3,
            output_tokens=1,
            finish_reason="stop",
            provider_name=model.provider,
            request_id="req_provider_order",
            usage_estimated=False,
        )

    monkeypatch.setattr(ProviderClient, "chat", fake_chat)

    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "models": [
                "openai/gpt-4o-mini",
                "mistral/mistral-small-2603",
                "deepseek/deepseek-v4-flash",
            ],
            "provider": {
                "order": ["mistral", "deepseek"],
                "only": ["openai", "mistral", "deepseek"],
                "sort": "price",
                "allow_fallbacks": False,
            },
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200, resp.text
    assert attempts == ["mistral/mistral-small-2603"]
    assert resp.json()["model"] == "mistral/mistral-small-2603"


def test_provider_failure_records_benchmark_without_generation(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def fail_chat(_self, model, _body):
        raise ProviderError(model.provider, 429, "rate limited")

    monkeypatch.setattr(ProviderClient, "chat", fail_chat)

    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 429
    assert not STORE.generation_store.generations
    samples = STORE.provider_benchmark_samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample.status == "error"
    assert sample.model == "openai/gpt-4o-mini"
    assert sample.provider == "openai"
    assert sample.error_status == 429
    assert sample.error_type == "provider_rate_limited"
    assert sample.total_cost_microdollars == 0
    assert sample.output_tokens == 0
    assert sample.elapsed_milliseconds is not None


def test_streaming_models_array_falls_back_before_first_chunk(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    attempts: list[str] = []

    def fake_stream_chat(_self, model, _body, state):
        attempts.append(model.id)

        async def iterator():
            if model.id == "deepseek/deepseek-v4-flash":
                raise ProviderError(model.provider, 503, "down")
            state.request_id = "req_stream_fallback"
            state.input_tokens = 5
            state.output_tokens = 2
            state.usage_estimated = False
            state.record_text("ok")
            yield b'data: {"id":"req_stream_fallback","choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return iterator()

    monkeypatch.setattr(ProviderClient, "stream_chat", fake_stream_chat)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "deepseek/deepseek-v4-flash",
            "models": ["mistral/mistral-small-2603"],
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())

    assert attempts == ["deepseek/deepseek-v4-flash", "mistral/mistral-small-2603"]
    assert b'"selected_model":"mistral/mistral-small-2603"' in body
    assert b"req_stream_fallback" in body


def test_regions_endpoint_and_gateway_authorize_include_routing_metadata() -> None:
    app = create_app(
        Settings(
            environment="test",
            regions="us-central1,europe-west4,asia-northeast1",
            primary_region="europe-west4",
        )
    )
    local_client = TestClient(app)
    created = local_client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway"},
    ).json()

    regions = local_client.get("/v1/regions")
    authorize = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "trustedrouter/auto",
            "region": "asia-northeast1",
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert regions.status_code == 200
    assert [item["id"] for item in regions.json()["data"]] == [
        "us-central1",
        "europe-west4",
        "asia-northeast1",
    ]
    assert regions.json()["trustedrouter"]["primary_region"] == "europe-west4"
    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == "trustedrouter/auto"
    assert data["model"] == "anthropic/claude-opus-4.7"
    assert data["region"] == "asia-northeast1"
    assert len(data["route_candidates"]) >= 2
    assert data["route_candidates"][0]["model"] == "anthropic/claude-opus-4.7"
    assert any(item["model"] == "kimi/kimi-k2.6" for item in data["route_candidates"])


def test_gateway_authorize_honors_models_and_provider_filters() -> None:
    app = create_app(Settings(environment="test"))
    local_client = TestClient(app)
    created = local_client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway"},
    ).json()
    for provider in ("mistral", "deepseek"):
        configured = local_client.put(
            f"/v1/byok/providers/{provider}",
            headers={"x-trustedrouter-user": "alice@example.com"},
            json={"secret_ref": f"env://{provider.upper()}_API_KEY"},
        )
        assert configured.status_code == 201, configured.text

    authorize = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "openai/gpt-4o-mini",
            "models": ["mistral/mistral-small-2603", "deepseek/deepseek-v4-flash"],
            "provider": {"order": ["mistral"], "ignore": ["openai"], "usage": "byok"},
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["model"] == "mistral/mistral-small-2603"
    assert [item["model"] for item in data["route_candidates"]] == [
        "mistral/mistral-small-2603",
        "deepseek/deepseek-v4-flash",
    ]


def test_default_regions_include_eu_region(client: TestClient) -> None:
    response = client.get("/v1/regions")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    # The default region list is the marketing-facing set the home-page
    # map renders. Pin the must-haves rather than the exact list so adding
    # a region doesn't trip the test.
    assert ids[0] == "us-central1", "primary region must lead the list"
    for required in ("us-central1", "europe-west4", "asia-northeast1", "australia-southeast1"):
        assert required in ids, f"missing required region {required}"
    assert response.json()["trustedrouter"]["primary_region"] == "us-central1"
