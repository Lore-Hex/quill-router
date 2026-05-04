from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.openrouter_coverage import ROUTE_COVERAGE
from trusted_router.storage import STORE

TEST_BYOK_KMS_KEY_NAME = (
    "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
)


def test_catalog_prices_keep_integer_discount_and_exact_openrouter_decimal(client: TestClient) -> None:
    models = client.get("/v1/models")
    assert models.status_code == 200
    for model in models.json()["data"]:
        pricing = model["pricing"]
        trusted = model["trustedrouter"]

        assert isinstance(trusted["prompt_price_microdollars_per_million_tokens"], int)
        assert isinstance(trusted["completion_price_microdollars_per_million_tokens"], int)
        assert isinstance(trusted["published_prompt_price_microdollars_per_million_tokens"], int)
        assert isinstance(trusted["published_completion_price_microdollars_per_million_tokens"], int)
        assert trusted["discount_microdollars_per_million_tokens"] == 10_000
        assert (
            trusted["published_prompt_price_microdollars_per_million_tokens"]
            - trusted["prompt_price_microdollars_per_million_tokens"]
            == 10_000
        )
        assert (
            trusted["published_completion_price_microdollars_per_million_tokens"]
            - trusted["completion_price_microdollars_per_million_tokens"]
            == 10_000
        )
        assert pricing["prompt"] == _per_token_decimal(
            trusted["prompt_price_microdollars_per_million_tokens"]
        )
        assert pricing["completion"] == _per_token_decimal(
            trusted["completion_price_microdollars_per_million_tokens"]
        )


def test_key_limits_preserve_single_microdollar_precision(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "one microdollar", "limit": "0.000001"},
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["limit_microdollars"] == 1
    assert data["limit_remaining_microdollars"] == 1
    assert data["reserved_microdollars"] == 0

    patched = client.patch(
        f"/v1/keys/{data['hash']}",
        headers=user_headers,
        json={"limit": "0.000002"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["limit_microdollars"] == 2
    assert patched.json()["data"]["limit_remaining_microdollars"] == 2


def test_gateway_settle_repeat_cannot_charge_or_log_twice(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway exact"}).json()
    key_hash = created["data"]["hash"]
    workspace_id = created["data"]["workspace_id"]

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 20,
            "max_output_tokens": 5,
        },
    )
    assert authorize.status_code == 200, authorize.text
    authorization_id = authorize.json()["data"]["authorization_id"]

    first = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 20,
            "actual_output_tokens": 2,
            "request_id": "gw-once",
            "elapsed_seconds": 1,
        },
    )
    assert first.status_code == 200, first.text
    usage_after_first = STORE.credits[workspace_id].total_usage_microdollars
    generation_count_after_first = len(STORE.generation_store.generations)

    repeat = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 10_000_000,
            "actual_output_tokens": 10_000_000,
            "request_id": "gw-double-charge-attempt",
            "elapsed_seconds": 1,
        },
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["already_settled"] is True
    assert STORE.credits[workspace_id].total_usage_microdollars == usage_after_first
    assert len(STORE.generation_store.generations) == generation_count_after_first
    assert all(gen.request_id != "gw-double-charge-attempt" for gen in STORE.generation_store.generations.values())


def test_gateway_refund_repeat_cannot_restore_credit_twice(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway refund"}).json()
    key_hash = created["data"]["hash"]
    workspace_id = created["data"]["workspace_id"]
    starting_available = _available_microdollars(workspace_id)

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 20,
            "max_output_tokens": 5,
        },
    )
    assert authorize.status_code == 200, authorize.text
    assert _available_microdollars(workspace_id) < starting_available
    authorization_id = authorize.json()["data"]["authorization_id"]

    first = client.post("/v1/internal/gateway/refund", json={"authorization_id": authorization_id})
    repeat = client.post("/v1/internal/gateway/refund", json={"authorization_id": authorization_id})

    assert first.status_code == 200, first.text
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["already_settled"] is True
    assert _available_microdollars(workspace_id) == starting_available
    assert STORE.credits[workspace_id].reserved_microdollars == 0


def test_production_prompt_routes_are_absent_and_gateway_requires_internal_token() -> None:
    prod_client = TestClient(_production_app())

    prompt = prod_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "must not hit control plane"}],
        },
    )
    assert prompt.status_code == 404

    no_token = prod_client.post(
        "/v1/internal/gateway/authorize",
        json={"api_key_hash": "key_missing", "model": "openai/gpt-4o-mini"},
    )
    wrong_token = prod_client.post(
        "/v1/internal/gateway/authorize",
        headers={"x-trustedrouter-internal-token": "wrong"},
        json={"api_key_hash": "key_missing", "model": "openai/gpt-4o-mini"},
    )
    assert no_token.status_code == 401
    assert wrong_token.status_code == 401
    assert no_token.json()["error"]["type"] == "unauthorized"
    assert wrong_token.json()["error"]["type"] == "unauthorized"


def test_all_stubbed_coverage_routes_have_stable_error_shapes(client: TestClient) -> None:
    for item in ROUTE_COVERAGE:
        if item.kind not in {"stub", "deprecated-stub"}:
            continue
        response = client.request(item.method, f"/v1{_sample_path(item.path)}", json={})
        payload = response.json()
        if item.kind == "deprecated-stub":
            assert response.status_code == 410, item
            assert payload["error"]["type"] == "deprecated", item
        elif item.path.startswith("/private/"):
            assert response.status_code == 404, item
            assert payload["error"]["type"] == "private_models_not_supported", item
        else:
            assert response.status_code == 501, item
            assert payload["error"]["type"] == "endpoint_not_supported", item


def _production_app():
    internal_token = "prod" + "-internal-token"
    webhook_secret = "whsec_" + "test"
    stripe_key = "sk_" + "test_secret"
    return create_app(
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
            byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
        ),
        configure_store_arg=False,
        init_observability=False,
    )


def _available_microdollars(workspace_id: str) -> int:
    account = STORE.credits[workspace_id]
    return (
        account.total_credits_microdollars
        - account.total_usage_microdollars
        - account.reserved_microdollars
    )


def _per_token_decimal(microdollars_per_million: int) -> str:
    denominator = 1_000_000 * 1_000_000
    whole = microdollars_per_million // denominator
    fraction = microdollars_per_million % denominator
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:012d}".rstrip("0")


def _sample_path(path: str) -> str:
    return (
        path.replace("{author}", "sample")
        .replace("{slug}", "model")
        .replace("{hash}", "key_missing")
        .replace("{jobId}", "job_sample")
        .replace("{id}", "sample")
    )
