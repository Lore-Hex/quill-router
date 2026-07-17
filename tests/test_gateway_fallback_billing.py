from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.catalog import MODELS, default_endpoint_for_model, endpoint_for_id
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import token_cost_microdollars
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.storage import STORE, CreditAccount, Workspace, configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _client_and_key() -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway fallback", "include_byok_in_limit": True},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def test_gateway_authorize_fake_spanner_uses_typed_without_allowlist_settings() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_gcp_capability_authorize"
    store._write_entity("workspace", ws, Workspace(id=ws, name="GCP", owner_user_id="u"))
    store._write_entity(
        "credit",
        ws,
        CreditAccount(workspace_id=ws),
    )
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws,
        "shard": 0,
        "total_credits": 10_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, api_key = store.create_api_key(
        workspace_id=ws,
        name="gateway typed",
        creator_user_id="u",
    )
    configure_store(store)
    settings = Settings(environment="test")
    client = TestClient(
        create_app(settings, configure_store_arg=False, init_observability=False)
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": api_key.hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["credit_reservation_id"] in db.reservations
    assert ("reservation", data["credit_reservation_id"]) not in db.rows


def test_gateway_authorizes_every_liberty_alias_to_working_nemotron_hosts() -> None:
    client, key = _client_and_key()

    for model_id in (
        "trustedrouter/liberty-1.0",
        "trustedrouter/liberty-1.0-1m",
        "trustedrouter/liberty-2.0",
        "trustedrouter/liberty-3.0",
    ):
        authorize = client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key["hash"],
                "model": model_id,
                "estimated_input_tokens": 1,
                "max_output_tokens": 1,
            },
        )

        assert authorize.status_code == 200, (model_id, authorize.text)
        routes = authorize.json()["data"]["route_candidates"]
        assert routes, model_id
        nemotron_hosts = {
            route["provider"]
            for route in routes
            if route["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
            and route["usage_type"] == "Credits"
        }
        assert {"baseten", "nebius"} <= nemotron_hosts, (model_id, routes)
        assert not {"together", "gmi"} & nemotron_hosts, (model_id, routes)


def test_gateway_settle_ancient_legacy_reservation_missing_typed_row_is_clean() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_gcp_ancient_legacy_settle"
    store._write_entity("workspace", ws, Workspace(id=ws, name="GCP", owner_user_id="u"))
    store._write_entity(
        "credit",
        ws,
        CreditAccount(workspace_id=ws),
    )
    _raw, api_key = store.create_api_key(
        workspace_id=ws,
        name="gateway legacy",
        creator_user_id="u",
    )
    model = MODELS["anthropic/claude-opus-4.7"]
    endpoint = default_endpoint_for_model(model)
    assert endpoint is not None
    auth = store.create_gateway_authorization(
        workspace_id=ws,
        key_hash=api_key.hash,
        model_id=model.id,
        provider=endpoint.provider,
        usage_type="Credits",
        estimated_microdollars=100_000,
        credit_reservation_id="legacy-reservation-never-in-typed",
        requested_model_id=model.id,
        candidate_model_ids=[model.id],
        region="us-central1",
        endpoint_id=endpoint.id,
        candidate_endpoint_ids=[endpoint.id],
    )
    assert auth.credit_reservation_id not in db.reservations
    configure_store(store)
    client = TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth.id,
            "selected_endpoint": endpoint.id,
            "actual_input_tokens": 1,
            "actual_output_tokens": 1,
            "request_id": "ancient-legacy-settle",
        },
    )

    assert settle.status_code == 200, settle.text
    assert settle.json()["data"] == {
        "authorization_id": auth.id,
        "settled": False,
        "already_settled": True,
    }
    assert auth.credit_reservation_id not in db.reservations


def test_gateway_settle_can_bill_authorized_fallback_model() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="mistral",
        secret_ref="env://MISTRAL_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="mis...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-opus-4.7",
            "models": ["mistralai/mistral-small-2603"],
            "estimated_input_tokens": 20_000,
            "max_output_tokens": 10_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["model"] == "anthropic/claude-opus-4.7"
    assert auth_data["limit_usage_type"] == "Credits"
    route_models = [item["model"] for item in auth_data["route_candidates"]]
    assert route_models[0] == "anthropic/claude-opus-4.7"
    assert "mistralai/mistral-small-2603" in route_models
    mistral_byok = next(
        item
        for item in auth_data["route_candidates"]
        if item["model"] == "mistralai/mistral-small-2603" and item["usage_type"] == "BYOK"
    )
    assert mistral_byok["byok_secret_ref"] == "env://MISTRAL_API_KEY"  # noqa: S105

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_endpoint": mistral_byok["endpoint_id"],
            "actual_input_tokens": 12_345,
            "actual_output_tokens": 6_789,
            "request_id": "gw-fallback-mistral",
            "elapsed_seconds": 1.5,
        },
    )

    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    expected_cost = cost_microdollars(MODELS["mistralai/mistral-small-2603"], 12_345, 6_789)
    assert data["model"] == "mistralai/mistral-small-2603"
    assert data["provider"] == "mistral"
    assert data["usage_type"] == "BYOK"
    assert data["limit_usage_type"] == "Credits"
    assert data["cost_microdollars"] == expected_cost

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.model == "mistralai/mistral-small-2603"
    assert generation.usage_type == "BYOK"
    assert generation.total_cost_microdollars == expected_cost

    money = STORE.credit_money[key["workspace_id"]]
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert refreshed_key is not None
    assert money.reserved_microdollars == 0
    assert money.total_usage_microdollars == 0
    assert refreshed_key.reserved_microdollars == 0
    assert refreshed_key.usage_microdollars == 0
    assert refreshed_key.byok_usage_microdollars == expected_cost


def test_gateway_authorize_and_settle_embeddings_model() -> None:
    # The attested-enclave prod path: authorize an embedding model (which is
    # supports_chat=False), then settle it billing INPUT tokens only.
    client, key = _client_and_key()

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/text-embedding-3-large",
            "estimated_input_tokens": 1_000,
            # Embeddings have no completion; the enclave sends the schema
            # minimum (1). Completion price is 0, so the estimate is unaffected.
            "max_output_tokens": 1,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["model"] == "openai/text-embedding-3-large"
    assert auth_data["limit_usage_type"] == "Credits"
    credits = next(
        item
        for item in auth_data["route_candidates"]
        if item["provider"] == "openai" and item["usage_type"] == "Credits"
    )

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_endpoint": credits["endpoint_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 0,
            "request_id": "gw-embeddings-openai",
            "elapsed_seconds": 0.2,
        },
    )
    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    expected_cost = cost_microdollars(MODELS["openai/text-embedding-3-large"], 1_000, 0)
    assert data["model"] == "openai/text-embedding-3-large"
    assert data["provider"] == "openai"
    assert data["cost_microdollars"] == expected_cost

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.tokens_prompt == 1_000
    assert generation.tokens_completion == 0


def test_gateway_validate_checks_key_without_reserving_or_recording_usage() -> None:
    client, key = _client_and_key()

    validate = client.post(
        "/v1/internal/gateway/validate",
        json={"api_key_hash": key["hash"], "route_type": "responses.input_tokens"},
    )

    assert validate.status_code == 200, validate.text
    assert validate.json()["data"] == {
        "workspace_id": key["workspace_id"],
        "api_key_hash": key["hash"],
        "route_type": "responses.input_tokens",
    }
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    money = STORE.credit_money[key["workspace_id"]]
    assert refreshed_key is not None
    assert refreshed_key.reserved_microdollars == 0
    assert refreshed_key.usage_microdollars == 0
    assert money.reserved_microdollars == 0
    assert not STORE.generation_store.generations


def test_gateway_settle_rejects_unlisted_fallback_without_charge_or_generation() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 500,
            "max_output_tokens": 200,
        },
    )
    assert authorize.status_code == 200, authorize.text

    rejected = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorize.json()["data"]["authorization_id"],
            "model": "meta-llama/llama-3.1-8b-instruct",
            "actual_input_tokens": 500,
            "actual_output_tokens": 200,
        },
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["type"] == "bad_request"
    assert not STORE.generation_store.generations
    money = STORE.credit_money[key["workspace_id"]]
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert refreshed_key is not None
    assert money.total_usage_microdollars == 0
    assert refreshed_key.usage_microdollars == 0
    assert refreshed_key.byok_usage_microdollars == 0


def test_gateway_refund_records_provider_benchmark_without_generation() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="deepseek",
        secret_ref="env://DEEPSEEK_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="dee...1234",
    )
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "deepseek/deepseek-v4-flash",
            "estimated_input_tokens": 500,
            "max_output_tokens": 200,
            "region": "europe-west4",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]

    refund = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_model": "deepseek/deepseek-v4-flash",
            "actual_input_tokens": 500,
            "actual_output_tokens": 0,
            "elapsed_seconds": 0.4,
            "streamed": True,
            "error_status": 503,
            "error_type": "provider_error",
        },
    )

    assert refund.status_code == 200, refund.text
    assert not STORE.generation_store.generations
    samples = STORE.provider_benchmark_samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample.status == "error"
    assert sample.model == "deepseek/deepseek-v4-flash"
    assert sample.provider == "deepseek"
    assert sample.region == "europe-west4"
    assert sample.error_status == 503
    assert sample.error_type == "provider_error"
    assert sample.total_cost_microdollars == 0
    assert sample.streamed is True


def test_gateway_missing_byok_primary_uses_prepaid_endpoint() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "mistralai/mistral-small-2603",
            "models": ["anthropic/claude-opus-4.7"],
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["model"] == "mistralai/mistral-small-2603"
    assert auth_data["usage_type"] == "Credits"
    assert auth_data["limit_usage_type"] == "Credits"
    assert auth_data["credit_reservation_id"]
    route_models = [item["model"] for item in auth_data["route_candidates"]]
    assert route_models[0] == "mistralai/mistral-small-2603"
    assert "anthropic/claude-opus-4.7" in route_models
    selected_endpoint_id = next(
        item["endpoint_id"]
        for item in auth_data["route_candidates"]
        if item["model"] == "anthropic/claude-opus-4.7"
        and item["usage_type"] == "Credits"
    )

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_model": "anthropic/claude-opus-4.7",
            "selected_endpoint": selected_endpoint_id,
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "request_id": "gw-fallback-credit",
        },
    )

    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    selected_endpoint = endpoint_for_id(selected_endpoint_id)
    assert selected_endpoint is not None
    expected_cost = token_cost_microdollars(
        1_000, selected_endpoint.prompt_price_microdollars_per_million_tokens
    ) + token_cost_microdollars(
        1_000, selected_endpoint.completion_price_microdollars_per_million_tokens
    )
    assert data["model"] == "anthropic/claude-opus-4.7"
    assert data["usage_type"] == "Credits"
    assert data["cost_microdollars"] == expected_cost

    money = STORE.credit_money[key["workspace_id"]]
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert refreshed_key is not None
    assert money.reserved_microdollars == 0
    assert money.total_usage_microdollars == expected_cost
    assert refreshed_key.usage_microdollars == expected_cost
    assert refreshed_key.byok_usage_microdollars == 0


def test_gateway_byok_preference_without_workspace_config_is_rejected() -> None:
    client, key = _client_and_key()

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "mistralai/mistral-small-2603",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 400
    assert authorize.json()["error"]["type"] == "provider_not_supported"
    assert not STORE.api_keys.gateway_authorizations
    assert not STORE.api_keys.reservations


def test_gateway_prepaid_route_does_not_return_byok_secret_even_if_configured() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="kimi",
        secret_ref="env://KIMI_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="kim...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "moonshotai/kimi-k2.6",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["usage_type"] == "Credits"
    assert data["byok_secret_ref"] is None
    assert data["byok_key_hint"] is None
    assert data["byok_encrypted_secret"] is None
    assert data["route_candidates"][0]["byok_secret_ref"] is None
    assert data["route_candidates"][0]["byok_encrypted_secret"] is None
    assert [item["usage_type"] for item in data["route_candidates"]][:2] == ["Credits", "BYOK"]
    assert {item["provider"] for item in data["route_candidates"]} >= {"kimi", "together"}


def test_gateway_can_prefer_byok_endpoint_for_dual_mode_model() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="kimi",
        secret_ref="env://KIMI_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="kim...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "moonshotai/kimi-k2.6",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["usage_type"] == "BYOK"
    assert data["limit_usage_type"] == "BYOK"
    assert data["credit_reservation_id"] is None
    assert data["byok_secret_ref"] == "env://KIMI_API_KEY"  # noqa: S105
    assert data["byok_key_hint"] == "kim...1234"
    assert data["route_candidates"] == [
        {
            "endpoint_id": data["endpoint_id"],
            "model": "moonshotai/kimi-k2.6",
            "upstream_model": "kimi-k2.6",
            "provider": "kimi",
            "provider_name": "Kimi",
            "usage_type": "BYOK",
            "byok_secret_ref": "env://KIMI_API_KEY",
            "byok_encrypted_secret": None,
            "byok_cache_key": None,
            "byok_key_hint": "kim...1234",
            "region": "us-central1",
        }
    ]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": data["authorization_id"],
            "selected_endpoint": data["endpoint_id"],
            "actual_input_tokens": 2_000,
            "actual_output_tokens": 1_000,
            "request_id": "gw-kimi-byok",
        },
    )

    assert settle.status_code == 200, settle.text
    settled = settle.json()["data"]
    assert settled["usage_type"] == "BYOK"
    assert settled["provider"] == "kimi"
    generation = STORE.get_generation(settled["generation_id"])
    assert generation is not None
    assert generation.usage_type == "BYOK"
    assert generation.provider == "kimi"
