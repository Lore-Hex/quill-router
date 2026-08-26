from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.providers import ProviderClient, ProviderError, ProviderResult
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import FAST_MODEL_ORDER, auto_candidate_models
from trusted_router.storage import STORE


def test_stablecoin_checkout_uses_stripe_crypto_payment_method(monkeypatch) -> None:
    app = create_app(Settings(environment="test", stripe_secret_key="sk_test"))  # noqa: S106
    local_client = TestClient(app)
    captured: dict[str, object] = {}

    def create_session(**kwargs):
        captured.update(kwargs)
        return {"id": "cs_crypto", "url": "https://checkout.stripe.test/crypto"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session
    )

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
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["workspace_id"] == data["workspace_id"]
    assert metadata["payment_method"] == "stablecoin"
    assert metadata["credit_amount_microdollars"] == "25000000"
    assert metadata["processing_fee_cents"] == "39"
    assert metadata["charge_amount_cents"] == "2539"


def test_trustedrouter_auto_rolls_over_to_next_provider(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    # Derived from the ladder, never hardcoded. Naming the models here is what
    # let auto's real leader drift away from the documented one unnoticed: the
    # assertions kept passing against a model nobody had chosen on purpose.
    ladder = [model.id for model in auto_candidate_models(None)]
    first, second = ladder[0], ladder[1]

    attempts: list[str] = []

    async def fake_chat(_self, model, _body):
        attempts.append(model.id)
        if model.id == first:
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
    assert attempts[:2] == [first, second]
    assert payload["model"] == second
    assert payload["trustedrouter"]["requested_model"] == "trustedrouter/auto"
    assert payload["trustedrouter"]["selected_model"] == second
    generation = next(iter(STORE.generation_store.generations.values()))
    assert generation.model == second


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
            "models": ["mistralai/mistral-small-2603", "openai/gpt-5.4-nano"],
            "provider": {"ignore": ["openai"]},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert attempts == ["deepseek/deepseek-v4-flash", "mistralai/mistral-small-2603"]
    assert payload["model"] == "mistralai/mistral-small-2603"
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
            "model": "mistralai/mistral-small-2603",
            "models": [
                "openai/gpt-5.4-nano",
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
    assert attempts == ["mistralai/mistral-small-2603"]
    assert resp.json()["model"] == "mistralai/mistral-small-2603"


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
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 429
    assert not STORE.generation_store.generations
    samples = STORE.provider_benchmark_samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample.status == "error"
    assert sample.model == "openai/gpt-5.4-nano"
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
            "models": ["mistralai/mistral-small-2603"],
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())

    assert attempts == ["deepseek/deepseek-v4-flash", "mistralai/mistral-small-2603"]
    assert b'"selected_model":"mistralai/mistral-small-2603"' in body
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
    auto_leader = auto_candidate_models(None)[0].id
    assert data["requested_model"] == "trustedrouter/auto"
    assert data["model"] == auto_leader
    assert data["region"] == "asia-northeast1"
    assert len(data["route_candidates"]) >= 2
    assert data["route_candidates"][0]["model"] == auto_leader
    # The exact tail of the auto rollover depends on which providers are
    # configured at request time; assert that at least one non-primary
    # candidate is present so callers know fallback is wired up.
    fallback_models = [
        item["model"] for item in data["route_candidates"] if item["model"] != auto_leader
    ]
    assert fallback_models, f"expected fallback candidates, got {data['route_candidates']}"


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
            "model": "openai/gpt-5.4-nano",
            "models": ["mistralai/mistral-small-2603", "deepseek/deepseek-v4-flash"],
            "provider": {"order": ["mistral"], "ignore": ["openai"], "usage": "byok"},
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["model"] == "mistralai/mistral-small-2603"
    assert [item["model"] for item in data["route_candidates"]] == [
        "mistralai/mistral-small-2603",
        "deepseek/deepseek-v4-flash",
    ]

    pinned = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "deepseek/deepseek-v4-flash",
            "models": ["openai/gpt-5.4-nano", "mistralai/mistral-small-2603"],
            "provider": {"order": ["deepseek"], "usage": "byok", "allow_fallbacks": False},
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert pinned.status_code == 200, pinned.text
    pinned_data = pinned.json()["data"]
    assert pinned_data["model"] == "deepseek/deepseek-v4-flash"
    assert pinned_data["provider"] == "deepseek"
    assert [item["model"] for item in pinned_data["route_candidates"]] == [
        "deepseek/deepseek-v4-flash",
    ]


def test_gateway_authorize_top_level_no_fallbacks_ignores_stale_alternatives() -> None:
    app = create_app(Settings(environment="test"))
    local_client = TestClient(app)
    created = local_client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway"},
    ).json()
    configured = local_client.put(
        "/v1/byok/providers/deepinfra",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"secret_ref": "env://DEEPINFRA_API_KEY"},
    )
    assert configured.status_code == 201, configured.text

    authorize = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "openai/gpt-oss-20b",
            "models": ["google/gemini-2.0-flash-lite"],
            "allow_fallbacks": False,
            "provider": {"only": ["deepinfra"], "usage": "byok"},
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == "openai/gpt-oss-20b"
    assert data["model"] == "openai/gpt-oss-20b"
    assert {item["model"] for item in data["route_candidates"]} == {
        "openai/gpt-oss-20b"
    }
    assert len(data["route_candidates"]) == 1


def test_gateway_no_fallbacks_selects_an_eligible_provider_for_exact_model() -> None:
    """Provider eligibility must run before pinning one exact-model endpoint."""
    settings = Settings(environment="test")
    raw_candidates = chat_route_endpoint_candidates(
        {
            "model": "openai/gpt-oss-20b",
            "provider": {"usage": "byok"},
        },
        settings,
    )
    first_provider = raw_candidates[0][1].provider
    eligible_provider = next(
        endpoint.provider
        for _model, endpoint in raw_candidates[1:]
        if endpoint.provider != first_provider
    )

    app = create_app(settings)
    local_client = TestClient(app)
    created = local_client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "exact-model@example.com"},
        json={"name": "exact-model"},
    ).json()
    configured = local_client.put(
        f"/v1/byok/providers/{eligible_provider}",
        headers={"x-trustedrouter-user": "exact-model@example.com"},
        json={"secret_ref": f"env://{eligible_provider.upper()}_API_KEY"},
    )
    assert configured.status_code == 201, configured.text

    authorize = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "openai/gpt-oss-20b",
            "models": ["google/gemini-2.0-flash-lite"],
            "allow_fallbacks": False,
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == "openai/gpt-oss-20b"
    assert data["model"] == "openai/gpt-oss-20b"
    assert data["provider"] == eligible_provider
    assert [candidate["model"] for candidate in data["route_candidates"]] == [
        "openai/gpt-oss-20b"
    ]
    assert [candidate["provider"] for candidate in data["route_candidates"]] == [
        eligible_provider
    ]


def test_gateway_authorize_expands_fast_router_pool() -> None:
    app = create_app(Settings(environment="test"))
    local_client = TestClient(app)
    created = local_client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway"},
    ).json()

    authorize = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "trustedrouter/fast",
            "estimated_input_tokens": 10,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == "trustedrouter/fast"
    route_candidates = data["route_candidates"]
    expected_models = [model_id for model_id in FAST_MODEL_ORDER if model_id in MODELS]
    assert expected_models
    assert data["model"] == expected_models[0]
    assert data["provider"] == route_candidates[0]["provider"]
    assert [item["model"] for item in route_candidates] == expected_models
    assert {item["provider"] for item in route_candidates} == {
        MODELS[model_id].provider for model_id in expected_models
    }


def test_default_regions_only_list_actual_attested_deployments(client: TestClient) -> None:
    """We only enumerate regions where a Confidential Space VM is
    actually deployed. Listing aspirational regions broke TLS for
    callers (cert SAN mismatch) and weakened the trust story —
    customers were sold "10 attested regions" but 8 were CNAME aliases
    to us-central1 with broken TLS. Today: 2 honest regions."""
    response = client.get("/v1/regions")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert ids[0] == "us-central1", "primary region must lead the list"
    assert "us-central1" in ids
    assert "europe-west4" in ids
    assert response.json()["trustedrouter"]["primary_region"] == "us-central1"
