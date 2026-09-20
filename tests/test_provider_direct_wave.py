"""Provider boundaries, exact prices, and fail-closed native discovery."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.pricing.openai_catalog import openai_model_price
from scripts.pricing.providers import (
    _direct_openai,
    general_compute,
    infomaniak,
    meta_direct,
    redpill,
)

META_PRICES = """<article><h3>Standard tier</h3><p>Models: <code>muse-spark-1.3</code>,
<code>muse-spark-1.1</code>.</p><div><table>
<tr><th>Usage</th><th>Price per 1M tokens</th></tr>
<tr><td>Cached input</td><td>$0.15</td></tr>
<tr><td>Input</td><td>$1.25</td></tr><tr><td>Output</td><td>$4.25</td></tr>
</table></div><h3>Contributor tier</h3><p><code>muse-spark-1.3-contributor</code></p>
<table><tr><td>Input</td><td>$0.10</td></tr><tr><td>Output</td><td>$0.20</td></tr></table></article>
"""
GENERAL_PRICES = """
| Model | Model ID | Context | Input / 1M tokens | Output / 1M tokens | Capabilities |
| **MiniMax M2.7** | `minimax-m2.7` | 192k | $0.28 | $1.20 | |
| DeepSeek V3.2 | `deepseek-v3.2` | 32k | $0.25 | $0.38 | Reasoning |
"""
INFO_PRICES = """
<div><div><p>mistralai/Ministral-3-14B-Instruct-2512</p></div>
<div>Input token: CHF 0.30 / 1M tokens Output token: CHF 0.40 / 1M tokens</div></div>
"""


def test_meta_uses_only_explicit_standard_tier_and_preserves_cache_price():
    payload = {"data": [{"id": name} for name in (
        "muse-spark-1.3", "muse-spark-1.1", "muse-spark-1.3-contributor", "muse-image-1.0",
    )]}
    rows = meta_direct.normalize_catalog(payload, META_PRICES)
    assert [r["id"] for r in rows] == ["muse-spark-1.3", "muse-spark-1.1"]
    price = openai_model_price(rows[0])
    assert price is not None
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (1_250_000, 4_250_000)
    assert price.tiers[0].prompt_cached_micro_per_m == 150_000
    assert rows[0]["context_length"] == 1_048_576


@pytest.mark.parametrize("html", ["", META_PRICES.replace("Standard tier", "Unknown"), META_PRICES.replace("$4.25", "$NaN"), META_PRICES.replace("1M tokens", "1K tokens")])
def test_meta_price_changes_fail_closed(html):
    with pytest.raises((RuntimeError, ArithmeticError)):
        meta_direct.normalize_catalog({"data": []}, html)


def test_general_compute_intersects_live_models_and_classifies_missing_prices():
    rows = general_compute.normalize_catalog(
        {"data": [{"id": "minimax-m2.7"}, {"id": "gemma-4-31B-it"}]}, GENERAL_PRICES,
    )
    assert len(rows) == 2
    price = openai_model_price(rows[0])
    assert price is not None
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (280_000, 1_200_000)
    assert rows[0]["context_length"] == 192_000
    assert "pricing" not in rows[1]
    with pytest.raises(RuntimeError, match="duplicate"):
        general_compute.normalize_catalog({"data": [{"id": "minimax-m2.7"}]}, GENERAL_PRICES * 2)
    with pytest.raises(RuntimeError, match="unit"):
        general_compute.normalize_catalog({"data": [{"id": "minimax-m2.7"}]}, GENERAL_PRICES.replace("1M tokens", "1K tokens"))


def test_infomaniak_ready_only_and_currency_conversion():
    row = {"id": 27, "name": "mistralai/Ministral-3-14B-Instruct-2512", "type": "llm", "info_status": "ready", "max_token_input": 100_000}
    payload = {"data": [row, {**row, "info_status": "coming_soon"}, {**row, "type": "embedding"}]}
    rows = infomaniak.normalize_catalog(payload, INFO_PRICES, Decimal("1.25"))
    assert len(rows) == 1
    price = openai_model_price(rows[0])
    assert price is not None
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (375_000, 500_000)
    with pytest.raises(RuntimeError, match="currency"):
        infomaniak.normalize_catalog(payload, INFO_PRICES.replace("CHF", "EUR"), Decimal("1.25"))
    with pytest.raises(RuntimeError, match="conversion"):
        infomaniak.normalize_catalog(payload, INFO_PRICES, Decimal("NaN"))


def test_redpill_cache_prices_and_phala_identity_are_independent():
    from scripts.pricing.providers import phala

    assert redpill.URL == "https://api.redpill.ai/v1/models"
    assert phala.URL == "https://inference.phala.com/v1/models"
    assert redpill.CATALOG.api_key_envs == ("REDPILL_API_KEY",)
    assert redpill.CATALOG.model_id("z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"
    price = openai_model_price({"pricing": {
        "prompt": "0.0000005", "completion": "0.000002",
        "input_cache_read": "0.0000001",
    }})
    assert price is not None
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (500_000, 2_000_000)
    assert price.tiers[0].prompt_cached_micro_per_m == 100_000


def test_phala_key_sync_never_falls_back_to_redpill():
    source = Path("scripts/deploy/secrets.sh").read_text()
    assert 'ensure_secret_from_env_file "PHALA_API_KEY" "trustedrouter-phala-api-key"\n' in source
    assert '"trustedrouter-phala-api-key" "REDPILL_API_KEY"' not in source


@pytest.mark.parametrize("model,field", [
    ("openai/gpt-5.6-sol", "max_completion_tokens"),
    ("openai/gpt-10", "max_completion_tokens"),
    ("openai/o3", "max_completion_tokens"),
    ("openai/gpt-oss-120b", "max_tokens"),
    ("openai/gpt-4o", "max_tokens"),
    ("z-ai/glm-5.3", "max_tokens"),
])
def test_redpill_canary_token_field_matches_native_model(model, field):
    assert redpill.max_tokens_field(model) == field


def test_redpill_fetch_forwards_model_specific_canary_cap(monkeypatch, tmp_path):
    rows = [{"id": model, "context_length": 128_000,
             "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
            for model in ("openai/gpt-5.6-sol", "z-ai/glm-5.3")]
    spec = replace(redpill.CATALOG.spec, catalog_loader=lambda _: rows)
    catalog = _direct_openai.DirectOpenAIProvider(spec, manifest_path=tmp_path / "models.json")
    monkeypatch.setenv("REDPILL_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: calls.append(kw) or True)
    catalog.fetch()
    assert {c["model"]: c["max_tokens_field"] for c in calls} == {
        "openai/gpt-5.6-sol": "max_completion_tokens", "z-ai/glm-5.3": "max_tokens",
    }
    assert all(c["max_tokens"] == 2048 for c in calls)


def test_reviewed_missing_price_never_probes_or_routes(monkeypatch, tmp_path):
    import json

    rows = general_compute.normalize_catalog(
        {"data": [{"id": "minimax-m2.7"}, {"id": "gemma-4-31B-it"}]}, GENERAL_PRICES,
    )
    spec = replace(general_compute.CATALOG.spec, catalog_loader=lambda _: rows)
    catalog = _direct_openai.DirectOpenAIProvider(spec, manifest_path=tmp_path / "models.json")
    monkeypatch.setenv("GENERAL_COMPUTE_KEY", "test-key")
    calls = []
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: calls.append(kw) or True)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)
    saved = {r["id"]: r for r in json.loads(catalog.manifest_path.read_text())["models"]}
    assert saved["google/gemma-4-31b-it"]["routable"] is False
    assert saved["google/gemma-4-31b-it"]["routable_reason"] == "price-unavailable"
    assert [c["model"] for c in calls] == ["minimax-m2.7"]


def test_new_providers_remain_standard_and_blocked_transports_stay_dark():
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS

    for slug in ("redpill", "meta-direct", "general-compute", "infomaniak", "privatemode", "swisscom"):
        provider = PROVIDERS[slug]
        assert provider.provider_e2ee is False
        assert provider.provider_zero_data_retention is False
        assert provider.supports_byok is False
    for slug in ("privatemode", "swisscom"):
        assert not PROVIDERS[slug].supports_prepaid
        assert slug not in GATEWAY_PREPAID_PROVIDER_SLUGS


def test_hourly_refresh_and_native_discovery_cover_new_paid_providers():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS, _scraper_slugs
    from scripts.pricing import refresh

    discovered = {r[0] for r in _DISCOVERABLE_MANIFEST_PROVIDERS}
    for slug in ("redpill", "meta-direct", "general-compute", "infomaniak"):
        assert slug in discovered
        assert slug in _scraper_slugs()
        assert Path(f"scripts/pricing/providers/{slug.replace('-', '_')}.py").is_file()
        module_name = refresh._result_slug_for_provider(slug)
        assert module_name in refresh.PROVIDER_SLUGS
        assert refresh._import_provider(module_name).SLUG == slug


def test_native_manifest_routes_preserve_identity_price_floor_and_holds():
    import json

    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.catalog_data import _UNSERVED_CREDITS_MODELS

    for slug in ("redpill", "meta-direct", "general-compute", "infomaniak"):
        manifest = json.loads(Path(f"src/trusted_router/data/provider_models/{slug}.json").read_text())
        for row in manifest["models"]:
            routes = [e for e in MODEL_ENDPOINTS.values() if e.provider == slug and e.model_id == row["id"]]
            if not row["routable"]:
                assert routes == [], (slug, row["id"])
                continue
            if row["id"] in _UNSERVED_CREDITS_MODELS:
                assert routes == [], row["id"]
                continue
            if slug == "redpill" and (
                row["id"].startswith("anthropic/")
                or row["id"] == "deepseek/deepseek-v4-pro-0813"
            ):
                # Existing first-party Claude policy and immutable 0813 route
                # membership still apply after a successful upstream probe.
                assert routes == [], row["id"]
                continue
            assert len(routes) == 1, (slug, row["id"])
            endpoint = routes[0]
            assert endpoint.usage_type == "Credits"
            assert endpoint.upstream_id == row["upstream_id"]
            assert endpoint.prompt_price_microdollars_per_million_tokens >= row["input_token_price_per_m"]
            assert endpoint.completion_price_microdollars_per_million_tokens >= row["output_token_price_per_m"]
            if "cached_input_token_price_per_m" in row:
                assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens >= row["cached_input_token_price_per_m"]


@pytest.mark.parametrize("stream", [False, True])
async def test_redpill_python_adapter_preserves_cap_for_both_modes(httpx_mock, stream):
    import json

    from trusted_router.catalog import Model
    from trusted_router.provider_adapters import (
        openai_compatible_chat,
        openai_compatible_chat_stream,
    )
    from trusted_router.provider_types import ProviderStreamState

    model = Model(id="openai/gpt-5.6-sol", name="GPT 5.6 Sol", provider="redpill",
                  upstream_id="openai/gpt-5.6-sol", context_length=128_000)
    url = "https://api.redpill.ai/v1/chat/completions"
    if stream:
        httpx_mock.add_response(url=url, text='data: {"choices":[{"delta":{"content":"PONG"}}]}\n\ndata: [DONE]\n\n')
    else:
        httpx_mock.add_response(url=url, json={"choices": [{"message": {"content": "PONG"}}]})
    request = {"messages": [{"role": "user", "content": "PONG"}], "max_tokens": 123, "temperature": 0}
    kwargs = {"api_key": "test-key", "base_url": "https://api.redpill.ai/v1"}
    if stream:
        state = ProviderStreamState(provider_name="Redpill", request_id="probe", input_tokens=1)
        chunks = [chunk async for chunk in openai_compatible_chat_stream(model, request, state, **kwargs)]
        assert b"PONG" in b"".join(chunks)
    else:
        result = await openai_compatible_chat(model, request, **kwargs)
        assert result.text == "PONG"
    sent = json.loads(httpx_mock.get_request().content)
    assert sent["max_completion_tokens"] == 123
    assert "max_tokens" not in sent
    assert "temperature" not in sent
