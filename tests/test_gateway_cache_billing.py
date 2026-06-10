"""Cache-aware settle billing.

The attested gateway reports cache_read_input_tokens /
cache_creation_input_tokens. Two things must hold:

1. Cached tokens are BILLED (pre-fix, Anthropic cache reads billed at
   zero because Anthropic's input_tokens exclude them) — at the
   provider's discounted multiple of the prompt price.
2. Provider semantics are normalized: Anthropic input_tokens EXCLUDE
   the cached tokens; OpenAI-compatible prompt counts INCLUDE them.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import cache_token_prices_microdollars, endpoint_for_id
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import token_cost_microdollars
from trusted_router.storage import STORE


def _client_and_key() -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "cache-bill@example.com"},
        json={"name": "cache billing"},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def _authorize(client: TestClient, key: dict, model: str) -> dict:
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": model,
            "estimated_input_tokens": 8_000,
            "max_output_tokens": 1_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    return authorize.json()["data"]


def test_anthropic_cache_read_and_write_tokens_are_billed() -> None:
    client, key = _client_and_key()
    auth = _authorize(client, key, "anthropic/claude-haiku-4.5")
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and endpoint.provider == "anthropic"

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            # Anthropic semantics: input_tokens EXCLUDE the cached tokens.
            "actual_input_tokens": 14,
            "actual_output_tokens": 6,
            "cache_read_input_tokens": 6081,
            "cache_creation_input_tokens": 2000,
            "request_id": "gw-cache-anthropic",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]

    prompt_price = endpoint.prompt_price_microdollars_per_million_tokens
    completion_price = endpoint.completion_price_microdollars_per_million_tokens
    read_price, write_price = cache_token_prices_microdollars("anthropic", prompt_price)
    assert read_price < prompt_price, "anthropic cache reads must be discounted"
    assert write_price > prompt_price, "anthropic cache writes cost more than raw input"
    expected = (
        token_cost_microdollars(14, prompt_price)
        + token_cost_microdollars(6, completion_price)
        + token_cost_microdollars(6081, read_price)
        + token_cost_microdollars(2000, write_price)
    )
    assert data["cost_microdollars"] == expected

    # Regression guard for the zero-billing bug: the cost must exceed what
    # the uncached 14 input tokens alone would have produced.
    uncached_only = token_cost_microdollars(14, prompt_price) + token_cost_microdollars(
        6, completion_price
    )
    assert data["cost_microdollars"] > uncached_only

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    # Dashboards see the TOTAL prompt, not Anthropic's exclusive count.
    assert generation.tokens_prompt == 14 + 6081 + 2000


def test_openai_compatible_cached_subset_is_normalized() -> None:
    client, key = _client_and_key()
    auth = _authorize(client, key, "mistralai/mistral-small-3.2-24b-instruct")
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and endpoint.provider != "anthropic"

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            # OpenAI-compatible semantics: prompt count INCLUDES cached.
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 50,
            "cache_read_input_tokens": 900,
            "request_id": "gw-cache-openai-compat",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]

    prompt_price = endpoint.prompt_price_microdollars_per_million_tokens
    completion_price = endpoint.completion_price_microdollars_per_million_tokens
    read_price, _ = cache_token_prices_microdollars(endpoint.provider, prompt_price)
    expected = (
        token_cost_microdollars(100, prompt_price)  # 1000 - 900 cached
        + token_cost_microdollars(50, completion_price)
        + token_cost_microdollars(900, read_price)
    )
    assert data["cost_microdollars"] == expected

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.tokens_prompt == 1_000


def test_settle_without_cache_fields_is_unchanged() -> None:
    client, key = _client_and_key()
    auth = _authorize(client, key, "anthropic/claude-haiku-4.5")
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 500,
            "actual_output_tokens": 100,
            "request_id": "gw-cache-none",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    expected = token_cost_microdollars(
        500, endpoint.prompt_price_microdollars_per_million_tokens
    ) + token_cost_microdollars(100, endpoint.completion_price_microdollars_per_million_tokens)
    assert data["cost_microdollars"] == expected
    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.tokens_prompt == 500
