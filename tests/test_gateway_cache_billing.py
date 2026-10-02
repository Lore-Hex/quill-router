"""Cache-aware settle billing.

The attested gateway reports cache_read_input_tokens /
cache_creation_input_tokens. Two things must hold:

1. Cached tokens are BILLED (pre-fix, Anthropic cache reads billed at
   zero because Anthropic's input_tokens exclude them). Reads bill at the
   route's published cached rate, or at the provider's discounted multiple
   of the prompt price when the route publishes none; writes bill at the
   provider's multiple.
2. Provider semantics are normalized: Anthropic input_tokens EXCLUDE
   the cached tokens; OpenAI-compatible prompt counts INCLUDE them.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests import catalog_vehicles
from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.catalog import cache_token_prices_microdollars, endpoint_for_id
from trusted_router.catalog_data import PriceTier
from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _customer_price
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary


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


def _serve_glm_53_with_a_cached_rate(monkeypatch: pytest.MonkeyPatch, provider_slug: str) -> None:
    """GLM 5.3 on one OpenAI-compatible host, with a published cached-input
    rate distinct from the provider's default cache multiple. A fixture: which
    hosts list GLM 5.3 today is provider state."""
    tier = PriceTier(
        max_prompt_tokens=None,
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=3_000_000,
        prompt_cached_price_microdollars_per_million_tokens=250_000,
    )
    serve_on_fixture_route(
        monkeypatch, "z-ai/glm-5.3", provider_slug, author="zai",
        price_tiers=(tier,), published_price_tiers=(tier,),
    )


def _authorize(
    client: TestClient,
    key: dict,
    model: str,
    *,
    provider: dict | None = None,
) -> dict:
    body = {
        "api_key_hash": key["hash"],
        "model": model,
        "estimated_input_tokens": 8_000,
        "max_output_tokens": 1_000,
    }
    if provider is not None:
        body["provider"] = provider
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json=body,
    )
    assert authorize.status_code == 200, authorize.text
    return authorize.json()["data"]


def test_anthropic_cache_read_and_write_tokens_are_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude Haiku 4.5 on a fixture route at $1/M input, $5/M output and a
    # published $0.08/M cache read, apart from Anthropic's 0.1x multiple: what
    # Anthropic charges today is provider state.
    tier = PriceTier(
        max_prompt_tokens=None,
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=5_000_000,
        prompt_cached_price_microdollars_per_million_tokens=80_000,
    )
    drop_routes(monkeypatch, "anthropic/claude-haiku-4.5")
    serve_on_fixture_route(
        monkeypatch, "anthropic/claude-haiku-4.5", "anthropic", author="anthropic",
        completion_price_microdollars_per_million_tokens=5_000_000,
        price_tiers=(tier,), published_price_tiers=(tier,),
    )
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

    # 14 input tokens at $1/M, 6 output at $5/M, 6,081 cache reads at the
    # published $0.08/M and 2,000 cache writes at Anthropic's 1.25x input.
    # Billing the cache tokens at zero, the regression, comes to 14 + 30.
    assert data["cost_microdollars"] == 14 + 30 + 486 + 2_500

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    # Dashboards see the TOTAL prompt, not Anthropic's exclusive count.
    assert generation.tokens_prompt == 14 + 6081 + 2000


@pytest.mark.parametrize("provider_slug", ["tinfoil", "featherless"])
def test_openai_compatible_cached_subset_is_normalized(
    provider_slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_glm_53_with_a_cached_rate(monkeypatch, provider_slug)
    client, key = _client_and_key()
    auth = _authorize(
        client,
        key,
        "z-ai/glm-5.3",
        provider={"only": [provider_slug]},
    )
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and endpoint.provider == provider_slug

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
    read_price = (
        endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
    )
    assert read_price is not None
    expected = (
        token_cost_microdollars(100, prompt_price)  # 1000 - 900 cached
        + token_cost_microdollars(50, completion_price)
        + token_cost_microdollars(900, read_price)
    )
    assert data["cost_microdollars"] == expected

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.tokens_prompt == 1_000
    assert generation.cached_input_tokens == 900

    balance = live_credit_summary(key["workspace_id"])
    replay = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 50,
            "cache_read_input_tokens": 900,
            "request_id": "gw-cache-openai-compat",
            "elapsed_seconds": 1.0,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["cost_microdollars"] == expected
    assert live_credit_summary(key["workspace_id"]) == balance


def test_tinfoil_glm_53_uses_published_cached_input_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_glm_53_with_a_cached_rate(monkeypatch, "tinfoil")
    client, key = _client_and_key()
    auth = _authorize(
        client,
        key,
        "z-ai/glm-5.3",
        provider={"only": ["tinfoil"]},
    )
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and endpoint.provider == "tinfoil"

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 50,
            "cache_read_input_tokens": 900,
            "request_id": "gw-cache-tinfoil",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text

    tier = endpoint.price_tiers[0]
    cached_price = tier.prompt_cached_price_microdollars_per_million_tokens
    assert cached_price is not None
    assert cached_price != cache_token_prices_microdollars(
        "tinfoil", tier.prompt_price_microdollars_per_million_tokens
    )[0], "fixture: the published rate must differ from the default multiple"
    expected = (
        token_cost_microdollars(
            100, tier.prompt_price_microdollars_per_million_tokens
        )
        + token_cost_microdollars(
            900,
            cached_price,
        )
        + token_cost_microdollars(
            50, tier.completion_price_microdollars_per_million_tokens
        )
    )
    assert settle.json()["data"]["cost_microdollars"] == expected


@pytest.mark.parametrize("provider_slug", ["tinfoil", "featherless"])
def test_glm_53_routes_publish_the_manifest_cached_input_rate(provider_slug: str) -> None:
    # Each routable row of the committed manifest, with the standard customer
    # markup; a delisted row publishes no route at all.
    raw = json.loads((_PROVIDER_MODELS_DIR / f"{provider_slug}.json").read_text(encoding="utf-8"))
    expected = {
        row["id"]: _customer_price(row["cached_input_token_price_per_m"])
        for row in raw["models"]
        if row["id"] == "z-ai/glm-5.3" and row.get("routable") is not False
    }
    endpoint = catalog_vehicles.registry_endpoints().get(f"z-ai/glm-5.3@{provider_slug}/prepaid")
    published = (
        {endpoint.model_id: endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens}
        if endpoint is not None
        else {}
    )
    assert published == expected


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
    assert generation.cached_input_tokens == 0


def test_settle_records_cache_reads_on_the_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generation's cached_input_tokens must reflect the settle body.

    Regression: `from_settle_body` only read the legacy `cached_input_tokens`
    / `cached_tokens` aliases, but the attested gateway sends
    `cache_read_input_tokens` (the name SettleRequest declares and billing
    reads). Every attested generation therefore recorded 0 cached tokens —
    across ~700k rows in production — making prompt-cache usage look
    nonexistent. Billing was always correct; only this metric was blank.
    """
    _serve_glm_53_with_a_cached_rate(monkeypatch, "tinfoil")
    client, key = _client_and_key()
    auth = _authorize(
        client,
        key,
        "z-ai/glm-5.3",
        provider={"only": ["tinfoil"]},
    )

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 50,
            "cache_read_input_tokens": 900,
            "request_id": "gw-cache-metric",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text
    generation = STORE.get_generation(settle.json()["data"]["generation_id"])
    assert generation is not None
    assert generation.cached_input_tokens == 900


def test_uniform_policy_fallback_multipliers_match_published_provider_policy() -> None:
    """Providers with a confirmed flat published cache-read discount.

    These fire only when an endpoint carries no per-model cached price.
    Mistral publishes a flat -90% on cached input; Fireworks an automatic
    -50%; Alibaba Model Studio bills implicit cache hits at 20% of input.
    Before 2026-08-31 all three fell to the 1x default, so their cache
    reads billed at full prompt price while the provider charged us the
    discounted rate.
    """
    prompt_price = 1_000_000
    for provider, expected_read in (
        ("mistral", 100_000),
        ("fireworks", 500_000),
        ("alibaba", 200_000),
    ):
        read_price, _ = cache_token_prices_microdollars(provider, prompt_price)
        assert read_price == expected_read, provider


def test_unknown_provider_still_bills_cache_reads_at_full_prompt_price() -> None:
    """The conservative default must survive the fallback additions."""
    read_price, _ = cache_token_prices_microdollars("nebius", 1_000_000)
    assert read_price == 1_000_000
