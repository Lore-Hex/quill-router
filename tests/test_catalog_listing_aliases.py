from dataclasses import replace

from trusted_router.catalog_aliases import (
    MODEL_LISTING_ALIASES,
    canonical_listing_id,
    consolidate_model_routes,
)
from trusted_router.catalog_data import Model, ModelEndpoint


def test_consolidation_preserves_legacy_routes_and_canonical_tariff() -> None:
    canonical = "qwen/qwen3.8-27b"
    models = {name: Model(name, name, "cerebras", 1000) for name in (*MODEL_LISTING_ALIASES, canonical)}
    original = {
        f"{alias}@{provider}/prepaid": ModelEndpoint(
            f"{alias}@{provider}/prepaid", alias, provider, "Credits",
            upstream_id=native, prompt_price_microdollars_per_million_tokens=123,
        )
        for alias, provider, native in (
            ("qwen/qwen-3-8-27b", "venice", "qwen-3-8-27b"),
            ("lightning-ai/qwen3.8-27b", "lightning", "lightning-ai/Qwen3.8-27B"),
            ("cerebras/qwen-3.8-27b", "cerebras", "qwen-3.8-27b"),
        )
    }
    existing = ModelEndpoint(f"{canonical}@cerebras/prepaid", canonical, "cerebras", "Credits",
                             upstream_id="qwen-3.8-27b", prompt_price_microdollars_per_million_tokens=456)
    endpoints = {**original, existing.id: existing}
    consolidate_model_routes(models, endpoints)
    for identity, endpoint in original.items():
        assert endpoints[identity] is endpoint
        target_id = f"{canonical}@{endpoint.provider}/prepaid"
        if endpoint.provider != "cerebras":
            assert endpoints[target_id] == replace(endpoint, id=target_id, model_id=canonical)
    assert endpoints[existing.id] is existing
    before = dict(endpoints)
    consolidate_model_routes(models, endpoints)
    assert endpoints == before


def test_listing_alias_requires_existing_target_and_never_guesses() -> None:
    alias = "qwen/qwen-3-8-27b"
    assert canonical_listing_id(alias, {}) == alias
    assert canonical_listing_id(alias, {"qwen/qwen3.8-27b": object()}) == "qwen/qwen3.8-27b"
    unknown = "qwen/qwen3.8-27b-fp8-turbo"
    assert canonical_listing_id(unknown, {"qwen/qwen3.8-27b": object()}) == unknown


def test_published_catalog_has_one_qwen_listing_with_all_existing_routes() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoints_for_model
    from trusted_router.public_catalog_aliases import canonical_public_model_id
    from trusted_router.routes.catalog import _public_catalog_payload

    rows = _public_catalog_payload((0, "listing-alias-regression")).shapes
    assert sum(row["id"] == "qwen/qwen3.8-27b" for row in rows) == 1
    assert not set(MODEL_LISTING_ALIASES).intersection(row["id"] for row in rows)
    for alias, canonical in MODEL_LISTING_ALIASES.items():
        if alias not in MODELS or canonical not in MODELS:
            continue
        assert sum(row["id"] == canonical for row in rows) == 1
        routes = {(e.provider, e.usage_type, e.upstream_id) for e in endpoints_for_model(canonical)}
        assert canonical_public_model_id(alias, MODELS) == canonical
        for endpoint in endpoints_for_model(alias):
            assert (endpoint.provider, endpoint.usage_type, endpoint.upstream_id) in routes
            assert MODEL_ENDPOINTS[endpoint.id] == endpoint
            # A copied route carries what the alias's route is reviewed to
            # accept: a tools request naming the canonical id must route.
            suffix = "byok" if endpoint.is_byok else "prepaid"
            copy = MODEL_ENDPOINTS[f"{canonical}@{endpoint.provider}/{suffix}"]
            if copy.upstream_id == endpoint.upstream_id:
                assert copy.supported_parameters == endpoint.supported_parameters
                assert copy.input_modalities == endpoint.input_modalities


def test_published_catalog_lists_no_ids_differing_only_in_case() -> None:
    from trusted_router.routes.catalog import _public_catalog_payload

    rows = _public_catalog_payload((0, "case-duplicate-regression")).shapes
    folded = [row["id"].casefold() for row in rows]
    assert len(folded) == len(set(folded))


def test_published_cache_read_never_exceeds_the_headline_prompt() -> None:
    from trusted_router.routes.catalog import _public_catalog_payload

    for row in _public_catalog_payload((0, "cache-price-regression")).shapes:
        pricing = row["pricing"]
        if "input_cache_read" in pricing and "prompt" in pricing:
            assert float(pricing["input_cache_read"]) <= float(pricing["prompt"]), row["id"]


def test_headline_cache_read_is_the_headline_routes_own() -> None:
    from trusted_router.catalog import MODELS, endpoints_for_model, model_to_openrouter_shape
    from trusted_router.money import microdollars_per_million_tokens_to_token_decimal

    # Nscale is the cheapest prompt and states no cache rate; Telnyx's cache
    # rate (above Nscale's prompt) must not be paired with Nscale's prompt.
    model = MODELS["qwen/qwen3-235b-a22b"]
    pricing = model_to_openrouter_shape(model)["pricing"]
    assert "input_cache_read" not in pricing
    # Where the headline route states one, it is published.
    claude = model_to_openrouter_shape(MODELS["anthropic/claude-sonnet-4.5"])["pricing"]
    cheapest = min(endpoints_for_model("anthropic/claude-sonnet-4.5"),
                   key=lambda e: e.prompt_price_microdollars_per_million_tokens)
    cached = cheapest.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
    assert claude["input_cache_read"] == microdollars_per_million_tokens_to_token_decimal(cached)
