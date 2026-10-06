from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.providers import lyceum


def card(model="z-ai/glm-5.3-flash", *, prompt="$0.20 /1M", output="$0.50 /1M", cached="$0.05 /1M", categories="text,code", context="1M"):
    return (
        f'<div class="model-card" data-model-categories="{categories}" data-model-name="Test">'
        f'<p class="model-card-id">{model}</p>'
        f'<div><span>Context</span><p>{context}</p></div>'
        f'<div><span>Input</span><p>{prompt}</p></div>'
        + (f'<div><span>Output</span><p>{output}</p></div>' if output is not None else "")
        + (f'<div><span>Cached</span><p>{cached}</p></div>' if cached is not None else "")
        + '</div>'
    )


def catalog(*models):
    return {"data": [{"id": model, "owned_by": "lyceum"} for model in models]}


def test_exact_current_and_cached_rates_with_conservative_context():
    prices, rows = lyceum.discover(catalog("z-ai/glm-5.3-flash"), card())
    price = prices["z-ai/glm-5.3-flash"]
    assert price.prompt_micro_per_m == 200_000
    assert price.completion_micro_per_m == 500_000
    assert price.tiers[0].prompt_cached_micro_per_m == 50_000
    row = rows["z-ai/glm-5.3-flash"]
    assert row["context_length"] == 1_000_000
    assert row["max_output_tokens"] == 65_536
    assert "prompt_caching" in row["supported_features"]
    assert row["upstream_id"] == "z-ai/glm-5.3-flash"


def test_new_models_automatically_join_but_unavailable_and_unpriced_do_not():
    prices, rows = lyceum.discover(
        catalog("z-ai/glm-9", "lyceum/router", "z-ai/glm-9-instant"),
        card("z-ai/glm-9") + card("z-ai/glm-5.3-flash"),
    )
    assert set(prices) == {"z-ai/glm-9"}
    assert "z-ai/glm-5.3-flash" not in rows
    for model in ("lyceum/router", "z-ai/glm-9-instant"):
        assert rows[model]["routable"] is False
    assert rows["lyceum/router"]["routable_reason"] == "price-unavailable"
    assert rows["z-ai/glm-9-instant"]["routable_reason"] == "awaiting-price"


def test_embedding_uses_input_price_and_no_cache_guess():
    prices, rows = lyceum.discover(
        catalog("qwen/qwen3-embedding-8b"),
        card("qwen/qwen3-embedding-8b", prompt="$0.02 /1M", output=None, cached=None, categories="embedding", context="32K"),
    )
    assert prices["qwen/qwen3-embedding-8b"].prompt_micro_per_m == 20_000
    assert prices["qwen/qwen3-embedding-8b"].completion_micro_per_m == 0
    assert prices["qwen/qwen3-embedding-8b"].tiers[0].prompt_cached_micro_per_m is None
    assert rows["qwen/qwen3-embedding-8b"]["endpoints"] == ["embeddings"]
    assert rows["qwen/qwen3-embedding-8b"]["context_length"] == 32_000


@pytest.mark.parametrize("changes", [
    {"prompt": "$NaN /1M"}, {"prompt": "EUR0.2 /1M"},
    {"prompt": "$0.20 /1K"}, {"prompt": "$-1 /1M"},
    {"prompt": "$0 /1M"}, {"output": "$0 /1M"},
    {"cached": "$0.21 /1M"}, {"context": "unknown"},
    {"context": "0M"}, {"prompt": "$0.0000001 /1M"},
])
def test_invalid_price_and_context_fail_closed(changes):
    with pytest.raises(RuntimeError):
        lyceum.parse_cards(card(**changes))


@pytest.mark.parametrize("payload,html", [
    ({}, card()), (catalog(), card()), (catalog("z-ai/glm-9"), "<html>unavailable</html>"),
    (catalog("z-ai/glm-5.3-flash"), card() + card()),
    (catalog("z-ai/glm-5.3-flash", "z-ai/glm-5.3-flash"), card()),
])
def test_missing_duplicate_or_empty_sources_never_publish(payload, html):
    with pytest.raises(RuntimeError):
        lyceum.discover(payload, html)


def test_failed_refresh_cannot_publish_old_discovery(monkeypatch):
    monkeypatch.delenv("LYCEUM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        lyceum.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        lyceum.write_provider_manifest(None)


def test_canaries_gate_publication_and_price_coverage(monkeypatch, tmp_path):
    path = tmp_path / "lyceum.json"
    monkeypatch.setattr(lyceum, "MANIFEST_PATH", path)
    monkeypatch.setenv("LYCEUM_API_KEY", "test-only-token")
    monkeypatch.setattr(lyceum, "fetch_json", lambda *_a, **_kw: catalog("z-ai/glm-5.3-flash", "moonshotai/kimi-k3", "lyceum/router", "qwen/qwen3-embedding-8b"))
    monkeypatch.setattr(lyceum, "fetch_html", lambda *_a: card() + card("moonshotai/kimi-k3") + card("qwen/qwen3-embedding-8b", prompt="$0.02 /1M", output=None, cached=None, categories="embedding"))
    probes = []

    def probe(key, row):
        assert key == "test-only-token"
        probes.append(row["upstream_id"])
        return row["upstream_id"] != "moonshotai/kimi-k3"

    monkeypatch.setattr(lyceum, "_probe", probe)
    result = lyceum.fetch()
    lyceum.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert rows["z-ai/glm-5.3-flash"]["routable"] is True
    assert rows["qwen/qwen3-embedding-8b"]["routable"] is True
    assert rows["moonshotai/kimi-k3"]["routable"] is False
    assert rows["moonshotai/kimi-k3"]["routable_reason"] == "provider-canary-failed"
    assert rows["lyceum/router"]["routable"] is False
    assert len(probes) == 3
    assert result.price_index_model_ids == {"z-ai/glm-5.3-flash", "moonshotai/kimi-k3"}


@pytest.mark.parametrize("vector,tokens,expected", [
    ([0.1, -0.2], 2, True), ([], 2, False),
    ([True], 2, False), ([0.1], True, False), ([0.1], 0, False),
])
def test_embedding_canary_requires_billable_usage(monkeypatch, vector, tokens, expected):
    response = httpx.Response(200, request=httpx.Request("POST", lyceum.BASE_URL + "/embeddings"), json={"data": [{"embedding": vector}], "usage": {"prompt_tokens": tokens}})
    monkeypatch.setattr(lyceum.httpx, "post", lambda *_a, **_kw: response)
    assert lyceum._probe("test", {"model_type": "embedding", "upstream_id": "qwen/qwen3-embedding-8b"}) is expected


def test_provider_registration_and_native_secret_wiring():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
    from trusted_router.services.inference_errors import default_provider_secret_ref

    assert OPENAI_COMPATIBLE_PROVIDERS["lyceum"] == (("LYCEUM_API_KEY",), lyceum.BASE_URL)
    assert "lyceum" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert default_provider_secret_ref("lyceum") == "env://LYCEUM_API_KEY"
    assert PROVIDERS["lyceum"].supports_embeddings
    assert not PROVIDERS["lyceum"].provider_zero_data_retention
    assert any(row[0] == "lyceum" for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    root = Path(__file__).resolve().parents[1]
    assert "LYCEUM_API_KEY:trustedrouter-lyceum-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "LYCEUM_API_KEY" "trustedrouter-lyceum-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()


def test_shared_embedding_model_keeps_each_provider_route():
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.pricing import _customer_price

    model_id = "qwen/qwen3-embedding-8b"
    assert MODELS[model_id].supports_embeddings
    assert not MODELS[model_id].supports_chat
    endpoint = MODEL_ENDPOINTS[f"{model_id}@lyceum/prepaid"]
    assert endpoint.upstream_id == model_id
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(20_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == 0
    assert any(e.provider != "lyceum" and e.model_id == model_id for e in MODEL_ENDPOINTS.values())


@pytest.mark.parametrize("changes", [
    {"input_token_price_per_m": 0},
    {"output_token_price_per_m": 1},
    {"cached_input_token_price_per_m": 10_000},
    {"price_tiers": []},
    {"endpoints": ["embeddings", "chat/completions"]},
])
def test_unsupported_embedding_price_contract_gets_no_route(monkeypatch, tmp_path, changes):
    from trusted_router import catalog_ingest

    row = {
        "id": "qwen/qwen3-embedding-8b", "model_type": "embedding",
        "endpoints": ["embeddings"], "context_length": 32_000,
        "input_token_price_per_m": 20_000, "output_token_price_per_m": 0,
        **changes,
    }
    (tmp_path / "lyceum.json").write_text(json.dumps({"models": [row]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert not endpoints


def test_cached_usage_bills_native_cached_price_without_double_counting(monkeypatch, tmp_path):
    from tests.pinned_manifests import serve_manifest_rows
    from trusted_router.catalog import endpoint_for_id
    from trusted_router.pricing import _customer_price
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

    serve_manifest_rows(monkeypatch, tmp_path, "lyceum", [{
        "id": "z-ai/glm-5.3-flash", "model_type": "chat",
        "endpoints": ["chat/completions"], "context_length": 1_000_000,
        "input_token_price_per_m": 200_000, "output_token_price_per_m": 500_000,
        "cached_input_token_price_per_m": 50_000,
    }])
    endpoint = endpoint_for_id("z-ai/glm-5.3-flash@lyceum/prepaid")
    assert endpoint is not None
    # Counts from a real repeated-prefix probe; cached reads are a subset of input.
    # Stage D uses half-up rounding separately for each token class.
    expected = sum(
        (tokens * _customer_price(rate) + 500_000) // 1_000_000
        for tokens, rate in ((1103, 200_000), (1920, 50_000), (281, 500_000))
    )
    assert _endpoint_cost_microdollars(
        endpoint, 1103, 281, cache_read_tokens=1920,
    ) == expected
