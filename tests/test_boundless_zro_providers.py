from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.pricing.providers import boundless, zro


def boundless_product(native="mimo-v2.6-pro"):
    return {"@type": "Product", "sku": native, "name": native,
            "additionalProperty": [{"name": "serving", "value": True},
                                   {"name": "supports", "value": "text, reasoning, tool-use, image input"}],
            "offers": [{"priceSpecification": {"unitText": unit, "priceCurrency": "USD", "price": price}}
                       for unit, price in [("per 1M input tokens", 0.435),
                                           ("per 1M output tokens", 0.87),
                                           ("per 1M cached input tokens", 0.0036)]]}


def boundless_html(*products):
    return '<script type="application/ld+json">' + json.dumps({
        "@type": "ItemList", "itemListElement": [{"item": p} for p in products or [boundless_product()]],
    }) + '</script><span>Rounded display: Input $0.44 Cached $0.00</span>'


def boundless_catalog(native="mimo-v2.6-pro"):
    return {"data": [{"id": native, "context_length": 1048576, "pricing": {"input": .435, "output": .87}}]}


def zro_html(native="glm-5.3-flash"):
    return f'''<p>Prices are USD per 1M tokens.</p><table><thead><tr>
    <th>Model</th><th>Context</th><th>Input</th><th>Output</th><th>Cache read</th>
    </tr></thead><tbody><tr><td><a href="/docs/models/{native}">GLM</a><code>{native}</code></td>
    <td>1M</td><td>$0.15</td><td>$0.50</td><td>$0.03</td></tr></tbody></table>'''


def zro_catalog(*ids):
    return {"data": [{"id": native} for native in ids or ["glm-5.3-flash", "zro/glm-5.3-flash"]]}


def test_boundless_exact_jsonld_not_rounded_visible_prices():
    prices, rows = boundless.discover(boundless_catalog(), boundless_html())
    price = prices["xiaomi/mimo-v2.6-pro"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m, price.tiers[0].prompt_cached_micro_per_m) == (435000, 870000, 3600)
    assert rows["xiaomi/mimo-v2.6-pro"]["input_modalities"] == ["text", "image"]
    assert boundless.canonical_model_id("dsv4") == "deepseek/deepseek-v4-flash-0731"
    assert boundless.canonical_model_id("convaiinnovations/laya") is None


@pytest.mark.parametrize("change", ["currency", "negative", "nan", "cache", "units", "missing", "duplicate"])
def test_boundless_invalid_prices_fail_closed(change):
    p = boundless_product()
    if change == "currency":
        p["offers"][0]["priceSpecification"]["priceCurrency"] = "EUR"
    elif change in {"negative", "nan"}:
        p["offers"][0]["priceSpecification"]["price"] = -1 if change == "negative" else "NaN"
    elif change == "cache":
        p["offers"][2]["priceSpecification"]["price"] = 1
    elif change == "units":
        p["offers"][0]["priceSpecification"]["unitText"] = "per token"
    elif change == "missing":
        p["offers"].pop()
    else:
        p["offers"].append(copy.deepcopy(p["offers"][0]))
    with pytest.raises(RuntimeError):
        boundless.discover(boundless_catalog(), boundless_html(p))


def test_boundless_conflicting_api_rate_and_missing_context_fail():
    payload = boundless_catalog()
    payload["data"][0]["pricing"]["input"] = 1
    with pytest.raises(RuntimeError, match="disagree"):
        boundless.discover(payload, boundless_html())
    payload = boundless_catalog()
    payload["data"][0]["context_length"] = True
    with pytest.raises(RuntimeError, match="context"):
        boundless.discover(payload, boundless_html())


def test_zro_aliases_collapse_and_unpriced_private_retired_routes_stay_out():
    payload = zro_catalog("glm-5.3-flash", "zro/glm-5.3-flash", "auto", "zro/auto",
                          "deepseek-v4.1-private", "dolly1-security-private", "deepseek-v4-flash-0731", "future-model")
    prices, rows = zro.discover(payload, zro_html())
    assert set(prices) == {"z-ai/glm-5.3-flash"}
    assert rows["z-ai/glm-5.3-flash"]["upstream_id"] == "glm-5.3-flash"
    assert prices["z-ai/glm-5.3-flash"].tiers[0].prompt_cached_micro_per_m == 30000
    assert rows["deepseek/deepseek-v4-flash-0731"]["routable"] is False
    assert rows["zro/future-model"]["routable_reason"] == "awaiting-price"
    assert len(rows) == 3


@pytest.mark.parametrize("old,new", [("$0.15", "$-1"), ("$0.15", "$NaN"), ("$0.03", "$1"),
                                    ("USD", "EUR"), ("1M tokens", "1K tokens"), ("<td>1M", "<td>0M")])
def test_zro_invalid_units_prices_context_fail(old, new):
    with pytest.raises(RuntimeError):
        zro.discover(zro_catalog(), zro_html().replace(old, new))


@pytest.mark.parametrize("module,catalog,html,expected", [
    (boundless, boundless_catalog, boundless_html, "xiaomi/mimo-v2.6-pro"),
    (zro, zro_catalog, zro_html, "z-ai/glm-5.3-flash"),
])
def test_dynamic_discovery_and_canary_fail_closed(module, catalog, html, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "MANIFEST_PATH", tmp_path / "models.json")
    monkeypatch.setenv(module.SLUG.upper() + "_API_KEY", "fake-key")
    monkeypatch.setattr(module, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(module, "fetch_html", lambda *_a, **_kw: html())
    calls = []
    monkeypatch.setattr(module, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = module.fetch()
    module.write_provider_manifest(result)
    row = json.loads(module.MANIFEST_PATH.read_text())["models"][0]
    assert row["id"] == expected and row["routable"] is False
    assert calls[0]["require_usage"] and calls[0]["expected_content"] == "PONG"
    monkeypatch.delenv(module.SLUG.upper() + "_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        module.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        module.write_provider_manifest(result)


@pytest.mark.parametrize("module,catalog,html", [(boundless, boundless_catalog, boundless_html), (zro, zro_catalog, zro_html)])
def test_empty_duplicate_catalog_and_new_versions(module, catalog, html):
    with pytest.raises(RuntimeError):
        module.discover({"data": []}, html())
    payload = catalog()
    payload["data"].append(copy.deepcopy(payload["data"][0]))
    with pytest.raises(RuntimeError):
        module.discover(payload, html())
    native = "glm-5.4"
    new_html = boundless_html(boundless_product(native)) if module is boundless else zro_html(native)
    assert "z-ai/glm-5.4" in module.discover(catalog(native), new_html)[0]


def test_registration_and_published_pricing(client):
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    root = Path(__file__).resolve().parents[1]
    for module in (boundless, zro):
        slug = module.SLUG
        assert slug in GATEWAY_PREPAID_PROVIDER_SLUGS
        assert OPENAI_COMPATIBLE_PROVIDERS[slug][1] == module.BASE_URL
        assert any(item[0] == slug for item in _DISCOVERABLE_MANIFEST_PROVIDERS)
        assert f"{slug.upper()}_API_KEY:trustedrouter-{slug}-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
        endpoint = MODEL_ENDPOINTS[f"z-ai/glm-5.3-flash@{slug}/prepaid"]
        assert endpoint.upstream_id == "glm-5.3-flash" and endpoint.usage_type == "Credits"
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens > 0
        page = client.get(f"/providers/{slug}")
        assert page.status_code == 200 and "glm-5.3-flash" in page.text and "Cached" in page.text
        assert not PROVIDERS[slug].provider_e2ee
    assert not PROVIDERS["boundless"].provider_zero_data_retention
    assert PROVIDERS["zro"].provider_zero_data_retention
    assert not PROVIDERS["hyperbolic"].supports_prepaid
    assert client.get("/providers/hyperbolic").status_code == 200
    assert not any(ep.provider == "hyperbolic" for ep in MODEL_ENDPOINTS.values())


def test_zro_duplicate_aliases_do_not_break_hourly_coverage(monkeypatch):
    from scripts import check_price_coverage as coverage

    monkeypatch.setattr(coverage, "_DISCOVERABLE_MANIFEST_PROVIDERS", (
        ("zro", zro.URL, ("ZRO_API_KEY",), zro.canonical_model_id),
    ))
    payload = zro_catalog("glm-5.3-flash", "zro/glm-5.3-flash", "auto", "zro/auto")
    warnings, info = coverage._model_discovery_audit(
        fetch_text=lambda _url: "",
        fetch_json=lambda url, _env: payload if url == zro.URL else {"data": []},
        published_model_ids=set(),
    )
    assert not [warning for warning in warnings if warning.startswith("zro:")]
    assert any(line.startswith("zro:") for line in info)


def test_zro_prefix_only_new_model_still_triggers_coverage(monkeypatch):
    from scripts import check_price_coverage as coverage

    monkeypatch.setattr(coverage, "_DISCOVERABLE_MANIFEST_PROVIDERS", (
        ("zro", zro.URL, ("ZRO_API_KEY",), zro.canonical_model_id),
    ))
    warnings, _ = coverage._model_discovery_audit(
        fetch_text=lambda _url: "",
        fetch_json=lambda url, _env: zro_catalog("zro/glm-5.4") if url == zro.URL else {"data": []},
        published_model_ids=set(),
    )
    assert any("zro:" in warning and "glm-5.4" in warning for warning in warnings)
