from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests import catalog_vehicles
from tests.pinned_manifests import CEREBRAS_GPT_OSS_120B
from trusted_router import catalog_ingest
from trusted_router.catalog import (
    _PROVIDER_DEPRECATED_UPSTREAM_MODELS,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    MODEL_ENDPOINTS,
    MODELS,
    ModelEndpoint,
    endpoints_for_model,
)
from trusted_router.catalog_ingest import (
    _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS,
    _PROVIDER_MODELS_DIR,
    _authoritative_provider_model_ids,
    _filter_unserved_provider_endpoints,
    _is_provider_deprecated_model,
    _provider_manifest_dark_model_ids,
)
from trusted_router.dashboard import _model_detail_view
from trusted_router.pricing import _customer_price
from trusted_router.provider_lifecycle import provider_model_retired


def _listed_row(provider: str, model_id: str) -> dict[str, Any] | None:
    """The provider's committed manifest row for a model it still lists. The
    hourly refresh marks a delisted row unroutable, so it has no route to check."""
    raw = json.loads((_PROVIDER_MODELS_DIR / f"{provider}.json").read_text(encoding="utf-8"))
    return next(
        (
            row
            for row in raw.get("models", [])
            if isinstance(row, dict)
            and row.get("id") == model_id
            and row.get("routable") is not False
            and not _is_provider_deprecated_model(
                provider, model_id, row.get("upstream_id") or model_id
            )
        ),
        None,
    )


def _delisted(endpoint_id: str) -> bool:
    """Whether the route's provider no longer lists its model."""
    model_id, _, route = endpoint_id.partition("@")
    return _listed_row(route.split("/")[0], model_id) is None


def _a_supplemental_priced_model() -> str:
    """A model with raw prepaid_available=False but a real Credits endpoint —
    i.e. a supplemental provider-native model that IS prepaid-routable."""
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            return model.id
    raise AssertionError("expected at least one supplemental priced model")


def test_supplemental_model_surfaces_as_prepaid_on_detail() -> None:
    model_id = _a_supplemental_priced_model()
    model = MODELS[model_id]
    # Premise: the raw catalog flag is a dedup marker (False)...
    assert model.prepaid_available is False
    # ...but the rendered detail view derives prepaid from endpoints → True.
    view = _model_detail_view(model)
    assert view["prepaid"] is True


def test_byok_only_model_stays_not_prepaid() -> None:
    # A model with no Credits endpoint and raw flag False must NOT flip to
    # prepaid (the `or model.prepaid_available` fallback is still conservative).
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if not any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            assert _model_detail_view(model)["prepaid"] is False
            return
    # If every non-prepaid model has a Credits endpoint, there's nothing to
    # assert — not a failure.


def test_model_detail_prices_credits_routes_once_and_shows_cached_input() -> None:
    model_id = "z-ai/glm-5.3-flash"
    endpoints = endpoints_for_model(model_id)
    credits = [endpoint for endpoint in endpoints if endpoint.usage_type == "Credits"]

    assert credits
    assert any(endpoint.usage_type == "BYOK" for endpoint in endpoints)
    assert any(
        tier.prompt_cached_price_microdollars_per_million_tokens is not None
        for endpoint in credits
        for tier in (endpoint.price_tiers or ())
    )

    view = _model_detail_view(MODELS[model_id])
    rendered_endpoints = view["endpoints"]

    assert isinstance(rendered_endpoints, list)
    assert {endpoint["endpoint_id"] for endpoint in rendered_endpoints} == {
        endpoint.id for endpoint in credits
    }
    assert all("usage_type" not in endpoint for endpoint in rendered_endpoints)
    assert view["cached_prompt_price"] != "Not published"
    assert any(
        endpoint["cached_prompt_price"] != "Not published"
        for endpoint in rendered_endpoints
    )


def _assert_a_listed_route_is_prepaid_at_its_price(provider: str, model_id: str) -> None:
    """A route the provider lists is prepaid at its published price plus the
    standard markup, on the exact upstream id the provider names."""
    row = _listed_row(provider, model_id)
    if row is None:
        return
    endpoint = MODEL_ENDPOINTS[f"{model_id}@{provider}/prepaid"]
    assert endpoint.upstream_id == (row.get("upstream_id") or model_id)
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(
        row["input_token_price_per_m"]
    )
    assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(
        row["output_token_price_per_m"]
    )
    if "cached_input_token_price_per_m" in row:
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == (
            _customer_price(row["cached_input_token_price_per_m"])
        )


def test_fireworks_glm53_flash_published_price_is_prepaid() -> None:
    for model_id in ("z-ai/glm-5.3-flash", "z-ai/glm-5.3-fast"):
        _assert_a_listed_route_is_prepaid_at_its_price("fireworks", model_id)


def test_wandb_glm53_flash_with_verified_price_is_prepaid() -> None:
    _assert_a_listed_route_is_prepaid_at_its_price("wandb", "z-ai/glm-5.3-flash")


def test_cerebras_only_credits_serves_allowlisted_models() -> None:
    # Cerebras's public account-callable feed is authoritative for Credits.
    # The generated manifest replaces a stale source allowlist so new models
    # become routable automatically without admitting unrelated OR inventory.
    allow = _authoritative_provider_model_ids("cerebras")
    cerebras_credits = {
        e.model_id
        for e in MODEL_ENDPOINTS.values()
        if e.provider == "cerebras" and e.usage_type == "Credits"
    }
    assert cerebras_credits == allow


def test_together_credits_follow_started_serverless_manifest() -> None:
    allow = _authoritative_provider_model_ids("together")
    together_credits = {
        e.model_id
        for e in catalog_vehicles.registry_endpoints().values()
        if e.provider == "together" and e.usage_type == "Credits"
    }
    # Together's authenticated feed changes as serverless models start and
    # retire. The generated manifest is the availability contract; freezing a
    # transient model ID here blocks every later catalog refresh after its
    # provider-confirmed retirement.
    assert together_credits == allow
    assert "meta-llama/llama-3.1-70b-instruct" not in together_credits


def test_dark_authoritative_manifest_rows_cannot_return_through_shared_snapshot() -> None:
    endpoint_pairs = {
        (endpoint.provider, endpoint.model_id)
        for endpoint in catalog_vehicles.registry_endpoints().values()
    }
    for provider_slug in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS:
        raw = json.loads(
            (_PROVIDER_MODELS_DIR / f"{provider_slug}.json").read_text(encoding="utf-8")
        )
        dark_models = {
            row["id"]
            for row in raw.get("models", [])
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row.get("routable") is False
        }
        assert not {
            (provider_slug, model_id)
            for model_id in dark_models
            if (provider_slug, model_id) in endpoint_pairs
        }


def test_dark_manifest_rows_cannot_return_as_prepaid_snapshot_routes() -> None:
    dark = _provider_manifest_dark_model_ids()
    endpoint_pairs = {
        (endpoint.provider, endpoint.model_id)
        for endpoint in catalog_vehicles.registry_endpoints().values()
        if endpoint.usage_type == "Credits"
    }
    assert not {
        (provider_slug, model_id)
        for provider_slug, model_ids in dark.items()
        for model_id in model_ids
        if (provider_slug, model_id) in endpoint_pairs
    }


def test_gmi_only_credits_serves_allowlisted_models() -> None:
    # GMI's /models listing has historically included routes that were not
    # callable on our account. Credits endpoints must stay within the paid-
    # canary-verified set; BYOK uses the customer's own key and keeps GMI's full
    # listing visible.
    allow = _PROVIDER_SERVED_MODEL_ALLOWLIST["gmi"]
    gmi_credits = {
        e.model_id
        for e in MODEL_ENDPOINTS.values()
        if e.provider == "gmi" and e.usage_type == "Credits"
    }
    assert gmi_credits <= allow
    unverified = "fixture/not-canary-verified"
    assert unverified not in allow
    routes = {
        f"{unverified}@gmi/{suffix}": ModelEndpoint(
            id=f"{unverified}@gmi/{suffix}", model_id=unverified, provider="gmi", usage_type=usage,
        )
        for suffix, usage in (("prepaid", "Credits"), ("byok", "BYOK"))
    }
    assert set(_filter_unserved_provider_endpoints(routes)) == {f"{unverified}@gmi/byok"}


def test_anthropic_models_credits_route_first_party_only() -> None:
    # Policy: Anthropic-authored (anthropic/*) models route via Anthropic
    # directly for Credits, never resellers (which list Claude ids they mostly
    # don't serve). BYOK is untouched — a customer's own reseller key is theirs.
    credits_providers = {
        e.provider
        for e in MODEL_ENDPOINTS.values()
        if e.model_id.startswith("anthropic/") and e.usage_type == "Credits"
    }
    assert credits_providers <= {"anthropic"}
    # Anthropic-direct Credits lineup stays fully routable.
    for model_id in _authoritative_provider_model_ids("anthropic"):
        assert f"{model_id}@anthropic/prepaid" in MODEL_ENDPOINTS, model_id
    # A reseller that lists Claude keeps its BYOK route but loses Credits.
    claude = "anthropic/claude-fixture"
    routes = {
        f"{claude}@lightning/{suffix}": ModelEndpoint(
            id=f"{claude}@lightning/{suffix}", model_id=claude, provider="lightning",
            usage_type=usage,
        )
        for suffix, usage in (("prepaid", "Credits"), ("byok", "BYOK"))
    }
    assert set(_filter_unserved_provider_endpoints(routes)) == {f"{claude}@lightning/byok"}


def test_cerebras_native_routes_use_verified_upstream_ids() -> None:
    raw = json.loads((_PROVIDER_MODELS_DIR / "cerebras.json").read_text(encoding="utf-8"))
    live_rows = [
        row
        for row in raw["models"]
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and row.get("routable") is not False
        and not provider_model_retired(
            "cerebras",
            row["id"],
            row.get("upstream_id"),
        )
    ]

    for row in live_rows:
        model_id = row["id"]
        upstream_id = row.get("upstream_id") or model_id
        for usage_type in ("prepaid", "byok"):
            endpoint = MODEL_ENDPOINTS[f"{model_id}@cerebras/{usage_type}"]
            assert endpoint.upstream_id == upstream_id


def test_nebius_deprecated_june_2026_models_are_not_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["nebius"]
    nebius_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "nebius"
    ]

    for endpoint in nebius_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated


def test_nebius_deprecation_does_not_remove_other_provider_routes() -> None:
    # Nebius's retirements name these model families' upstream ids. A host
    # that still lists the model keeps its route.
    for endpoint_id in (
        "minimax/minimax-m2.5@minimax/byok",
        "moonshotai/kimi-k2.6@kimi/prepaid",
        "openai/gpt-oss-120b@cerebras/prepaid",
        "z-ai/glm-5@zai/prepaid",
    ):
        assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_tinfoil_june_2026_deprecations_and_replacements_are_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["tinfoil"]
    tinfoil_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "tinfoil"
    ]

    for endpoint in tinfoil_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    # The replacements Tinfoil lists are prepaid on its own ids and prices.
    for model_id in ("z-ai/glm-5.3", "google/gemma-4-31b-it"):
        _assert_a_listed_route_is_prepaid_at_its_price("tinfoil", model_id)

    assert "z-ai/glm-5.1@tinfoil/prepaid" not in MODEL_ENDPOINTS
    assert "z-ai/glm-5.1@tinfoil/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-instruct@tinfoil/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-instruct@tinfoil/byok" not in MODEL_ENDPOINTS
    # Provider-scoped deprecation: non-Tinfoil routes for these model families
    # remain available when their provider still serves them.
    for endpoint_id in (
        "z-ai/glm-5.1@zai/prepaid",
        "qwen/qwen3-vl-30b-a3b-instruct@novita/prepaid",
    ):
        assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_novita_july_2026_retirements_and_replacements_are_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["novita"]
    novita_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "novita"
    ]

    for endpoint in novita_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    assert "deepseek/deepseek-r1-distill-qwen-14b@novita/prepaid" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-14b@novita/byok" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-32b@novita/prepaid" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-32b@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-next-80b-a3b-thinking@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-next-80b-a3b-thinking@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-thinking@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-thinking@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-8b-instruct@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-8b-instruct@novita/byok" not in MODEL_ENDPOINTS

    # The replacements are routable while Novita lists them.
    for model_id in (
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.6-27b",
        "qwen/qwen3.6-35b-a3b",
    ):
        for endpoint_id in (f"{model_id}@novita/prepaid", f"{model_id}@novita/byok"):
            assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_friendli_july_2026_glm_5_deprecation_does_not_remove_glm_52() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["friendli"]
    friendli_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "friendli"
    ]

    for endpoint in friendli_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    assert "z-ai/glm-5@friendli/prepaid" not in MODEL_ENDPOINTS
    assert "z-ai/glm-5@friendli/byok" not in MODEL_ENDPOINTS
    # Provider-scoped deprecation: GLM 5.2 on Friendli and other GLM-5 routes
    # remain available if their providers still serve them.
    for endpoint_id in (
        "z-ai/glm-5.2@friendli/prepaid",
        "z-ai/glm-5.2@friendli/byok",
        "z-ai/glm-5@zai/prepaid",
    ):
        assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_route_health_first_sweep_dead_routes_are_not_routable() -> None:
    flagged_routes = {
        ("openai/gpt-5.6-sol", "lightning"),
        ("x-ai/grok-4.5", "gmi"),
        ("anthropic/claude-opus-4.8", "phala"),
        ("z-ai/glm-5.1", "deepinfra"),
        ("moonshotai/kimi-k2", "kimi"),
    }

    for model_id, provider in flagged_routes:
        assert not [
            endpoint
            for endpoint in endpoints_for_model(model_id)
            if endpoint.provider == provider
        ]

    # Together's generated authoritative manifest supersedes the July 18
    # route-health quarantine: GPT OSS 120B is routable while Together's
    # serverless feed lists it. Quarantine is also provider-scoped: sibling
    # routes survive while their providers list them.
    for endpoint_id in (
        "openai/gpt-oss-120b@together/prepaid",
        "openai/gpt-oss-120b@cerebras/prepaid",
        "mistralai/mistral-small-24b-instruct-2501@deepinfra/prepaid",
    ):
        assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_glm_53_flash_publishes_all_verified_provider_routes() -> None:
    model_id = "z-ai/glm-5.3-flash"
    credits = {
        endpoint.provider: endpoint.upstream_id
        for endpoint in endpoints_for_model(model_id)
        if endpoint.usage_type == "Credits"
    }

    # Each verified host that lists the model has a Credits route on the exact
    # upstream id its manifest names.
    for provider in ("zai", "deepinfra", "io-net", "novita"):
        row = _listed_row(provider, model_id)
        if row is not None:
            assert credits.get(provider) == (row.get("upstream_id") or model_id), provider


def test_gemini_native_supplement_publishes_missing_text_models() -> None:
    # Each of these rows Google lists natively is a chat route on its exact
    # upstream id. AI Studio's row also sets the model's window and list price.
    # The two Google products have independent rates and discount schedules;
    # their price-index isolation is covered by test_vertex_native_discovery.
    for provider, model_id in (
        ("google-ai-studio", "google/gemini-3.5-flash"),
        ("google-ai-studio", "google/gemini-3.6-flash"),
        ("google-ai-studio", "google/gemini-3.1-flash-image-preview"),
        ("google-vertex", "google/gemini-3.6-flash"),
    ):
        row = _listed_row(provider, model_id)
        if row is None:
            continue
        endpoint = MODEL_ENDPOINTS[f"{model_id}@{provider}/prepaid"]
        assert MODELS[model_id].supports_chat
        assert endpoint.upstream_id == (row.get("upstream_id") or model_id)
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
        if "cached_input_token_price_per_m" in row:
            assert (
                endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
                < endpoint.prompt_price_microdollars_per_million_tokens
            )
        if provider == "google-ai-studio":
            assert MODELS[model_id].context_length == row["context_length"]
            assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(
                row["input_token_price_per_m"]
            )
            assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(
                row["output_token_price_per_m"]
            )


def test_google_products_have_distinct_capabilities() -> None:
    # Vertex routes are prepaid-only; AI Studio routes offer prepaid and BYOK,
    # while each product lists the model.
    for model_id in ("google/gemini-2.5-flash", "google/gemini-3.6-flash"):
        assert f"{model_id}@google-vertex/byok" not in MODEL_ENDPOINTS
        for endpoint_id in (
            f"{model_id}@google-vertex/prepaid",
            f"{model_id}@google-ai-studio/prepaid",
            f"{model_id}@google-ai-studio/byok",
        ):
            assert endpoint_id in MODEL_ENDPOINTS or _delisted(endpoint_id), endpoint_id


def test_llama_33_70b_no_longer_credits_routes_to_cerebras(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Regression for the cerebras 502s: this model's Credits route used to
    # include cerebras (which can't serve it) and fail. Its prepaid routing
    # must now use only providers that actually serve it: Cerebras's own feed
    # decides its routes, here one row of it as pinned, and a host that serves
    # the model keeps its route. Whether the model is still prepaid somewhere
    # today is test_llama_33_70b_is_still_prepaid_off_cerebras.
    (tmp_path / "cerebras.json").write_text(
        json.dumps({"provider": "cerebras", "models": [CEREBRAS_GPT_OSS_120B]}), encoding="utf-8"
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    routes = {
        f"{model_id}@{provider}/prepaid": ModelEndpoint(
            id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider,
            usage_type="Credits",
        )
        for model_id, provider in (
            ("meta-llama/llama-3.3-70b-instruct", "cerebras"),
            ("meta-llama/llama-3.3-70b-instruct", "parasail"),
            ("openai/gpt-oss-120b", "cerebras"),
        )
    }
    assert set(_filter_unserved_provider_endpoints(routes)) == {
        "meta-llama/llama-3.3-70b-instruct@parasail/prepaid",
        "openai/gpt-oss-120b@cerebras/prepaid",
    }


def test_novita_supplemental_prices_apply_manifest_scale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Novita's /models feed prices 100x smaller than its public $/Mt table, and
    # its manifest says so; the catalog applies that scale before the markup.
    committed = json.loads((_PROVIDER_MODELS_DIR / "novita.json").read_text(encoding="utf-8"))
    assert committed["price_scale_to_microdollars_per_million_tokens"] == 100
    (tmp_path / "novita.json").write_text(json.dumps({
        "provider": "novita", "price_scale_to_microdollars_per_million_tokens": 100,
        "models": [{
            "id": "qwen/qwen3-235b-a22b-instruct-2507",
            "upstream_id": "qwen/qwen3-235b-a22b-instruct-2507",
            "model_type": "chat", "endpoints": ["chat/completions"],
            "input_token_price_per_m": 900, "output_token_price_per_m": 5800,
        }],
    }))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    endpoint = endpoints["qwen/qwen3-235b-a22b-instruct-2507@novita/prepaid"]

    assert endpoint.prompt_price_microdollars_per_million_tokens == 94_950
    assert endpoint.completion_price_microdollars_per_million_tokens == 611_900
    assert endpoint.prompt_price_microdollars_per_million_tokens > 10_000


# Live provider state. provider-catalog-health.yml reports these hourly; the
# rules above hold whichever of these routes a provider delists.
@pytest.mark.provider_health
@pytest.mark.parametrize(
    "endpoint_id",
    [
        "anthropic/claude-fable-5@anthropic/prepaid",
        "z-ai/glm-5.3-flash@fireworks/prepaid",
        "z-ai/glm-5.3-fast@fireworks/prepaid",
        "z-ai/glm-5.2@friendli/prepaid",
        "z-ai/glm-5.2@friendli/byok",
        "z-ai/glm-5@zai/prepaid",
        "google/gemini-3.5-flash@google-ai-studio/prepaid",
        "google/gemini-3.6-flash@google-ai-studio/prepaid",
        "google/gemini-3.6-flash@google-ai-studio/byok",
        "google/gemini-3.6-flash@google-vertex/prepaid",
        "google/gemini-3.1-flash-image-preview@google-ai-studio/prepaid",
        "google/gemini-2.5-flash@google-ai-studio/prepaid",
        "google/gemini-2.5-flash@google-ai-studio/byok",
        "google/gemini-2.5-flash@google-vertex/prepaid",
        "z-ai/glm-5.3-flash@zai/prepaid",
        "z-ai/glm-5.3-flash@deepinfra/prepaid",
        "z-ai/glm-5.3-flash@io-net/prepaid",
        "z-ai/glm-5.3-flash@novita/prepaid",
        "minimax/minimax-m2.5@minimax/byok",
        "moonshotai/kimi-k2.6@kimi/prepaid",
        "openai/gpt-oss-120b@cerebras/prepaid",
        "openai/gpt-oss-120b@together/prepaid",
        "mistralai/mistral-small-24b-instruct-2501@deepinfra/prepaid",
        "z-ai/glm-5.3-flash@wandb/prepaid",
        "z-ai/glm-5.3@tinfoil/prepaid",
        "google/gemma-4-31b-it@tinfoil/prepaid",
        "z-ai/glm-5.1@zai/prepaid",
        "qwen/qwen3-vl-30b-a3b-instruct@novita/prepaid",
        "deepseek/deepseek-v4-flash@novita/prepaid",
        "deepseek/deepseek-v4-flash@novita/byok",
        "qwen/qwen3.6-27b@novita/prepaid",
        "qwen/qwen3.6-27b@novita/byok",
        "qwen/qwen3.6-35b-a3b@novita/prepaid",
        "qwen/qwen3.6-35b-a3b@novita/byok",
        "qwen/qwen3-235b-a22b-instruct-2507@novita/prepaid",
    ],
)
def test_the_routes_these_rules_were_written_against_are_still_served(endpoint_id: str) -> None:
    assert endpoint_id in MODEL_ENDPOINTS, f"{endpoint_id} is no longer served"


@pytest.mark.provider_health
def test_llama_33_70b_is_still_prepaid_off_cerebras() -> None:
    credits_providers = {
        e.provider
        for e in endpoints_for_model("meta-llama/llama-3.3-70b-instruct")
        if e.usage_type == "Credits"
    }
    assert "cerebras" not in credits_providers
    assert credits_providers & {"novita", "parasail", "tinfoil", "together"}


@pytest.mark.provider_health
@pytest.mark.parametrize(
    "provider", ["cerebras", "friendli", "gmi", "nebius", "novita", "tinfoil", "together"],
)
def test_the_providers_these_rules_cover_still_serve_credits(provider: str) -> None:
    assert any(
        endpoint.provider == provider and endpoint.usage_type == "Credits"
        for endpoint in MODEL_ENDPOINTS.values()
    ), f"{provider} serves no Credits route"
