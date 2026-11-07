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


def free_alias_card():
    # Shape of Boundless's 2026-11 free-tier alias card: not served, and its
    # offer has no priceSpecification at all.
    return {"@type": "Product", "sku": "minimax-m3-free", "name": "MiniMax M3",
            "additionalProperty": [{"name": "serving", "value": False},
                                   {"name": "supports", "value": "text, reasoning, tool-use"}],
            "offers": [{"@type": "Offer", "priceCurrency": "USD", "price": 0}]}


def test_boundless_unserved_card_without_rate_card_does_not_hold_the_provider():
    # It held every Boundless price from 2026-10-26 until the 14-day manifest
    # deadline (Sentry QUILL-ROUTER-F8).
    catalog = boundless_catalog()
    catalog["data"].append({"id": "minimax-m3-free", "context_length": 1048576})
    prices, rows = boundless.discover(catalog, boundless_html(boundless_product(), free_alias_card()))
    assert set(prices) == {"xiaomi/mimo-v2.6-pro"}
    free = rows[boundless.canonical_model_id("minimax-m3-free")]
    assert (free["routable"], free["routable_reason"]) == (False, "awaiting-price")


def test_boundless_served_card_without_rate_card_still_fails_closed():
    card = free_alias_card()
    card["additionalProperty"][0]["value"] = True
    with pytest.raises(RuntimeError, match="invalid price units or currency"):
        boundless.discover(boundless_catalog(), boundless_html(boundless_product(), card))


def test_boundless_conflicting_api_rate_and_missing_context_fail():
    payload = boundless_catalog()
    payload["data"][0]["pricing"]["input"] = 1
    with pytest.raises(RuntimeError, match="disagree"):
        boundless.discover(payload, boundless_html())
    payload = boundless_catalog()
    payload["data"][0]["context_length"] = True
    with pytest.raises(RuntimeError, match="context"):
        boundless.discover(payload, boundless_html())


BOUNDLESS_2026_11_07 = Path(__file__).parent / "fixtures/pricing/boundless_models_2026-11-07.html"
# Short identifiers Boundless served before its 2026-11 rate cards renamed
# every model to a qualified ID.
BOUNDLESS_LEGACY_IDS = ("dsv4", "qwen3.6", "nemotron3-super", "glm-5.3", "minimax-m3", "minimax-m3-free",
                        "convaiinnovations/laya")


def boundless_live_cards():
    html = BOUNDLESS_2026_11_07.read_text()
    document = json.loads(html.split('<script type="application/ld+json">', 1)[1].split("</script>", 1)[0])
    return html, [item["item"] for item in document["itemListElement"]]


def boundless_catalog_for(cards, extra_ids=()):
    rows = []
    for card in cards:
        served = {p["name"]: p["value"] for p in card["additionalProperty"]}.get("serving") is True
        spec = {o["priceSpecification"]["unitText"]: o["priceSpecification"]["price"]
                for o in card["offers"] if "priceSpecification" in o}
        if served and spec:
            rows.append({"id": card["sku"], "context_length": 1048576,
                         "pricing": {"input": spec["per 1M input tokens"], "output": spec["per 1M output tokens"]}})
    by_model = {boundless.canonical_model_id(row["id"]): row for row in rows}
    for native in extra_ids:
        twin = by_model.get(boundless.canonical_model_id(native))
        rows.append({**twin, "id": native} if twin else {"id": native, "context_length": 1048576})
    return {"data": rows}


def test_boundless_2026_11_rate_cards_with_legacy_alias_ids_parse():
    # Refresh run 42359425520 (2026-11-07) held Boundless on "duplicate
    # canonical model ID": the API lists a legacy short ID (dsv4) beside the
    # qualified ID the rate card now names (deepseek/deepseek-v4-flash-0731).
    html, cards = boundless_live_cards()
    prices, rows = boundless.discover(boundless_catalog_for(cards, BOUNDLESS_LEGACY_IDS), html)
    assert len(prices) == 24
    assert rows["deepseek/deepseek-v4-flash-0731"]["upstream_id"] == "deepseek/deepseek-v4-flash-0731"
    assert rows["z-ai/glm-5.3"]["upstream_id"] == "z-ai/glm-5.3"
    # Qualified IDs are not author-prefixed twice, and Nemotron uses the
    # repository-wide ID the card names.
    assert "qwen/qwen3.6-35b-a3b" in prices and "qwen/qwen/qwen3.6-35b-a3b" not in rows
    assert "nvidia/nemotron-3-super-120b-a12b" in prices and "nvidia/nemotron-3-super" not in rows
    assert prices["xiaomi/mimo-v2.6-pro"].tiers[0].prompt_cached_micro_per_m == 3600
    free = rows["minimax/minimax-m3-free"]
    assert (free["upstream_id"], free["routable"], free["routable_reason"]) == ("minimax-m3-free", False, "awaiting-price")
    assert "convaiinnovations/laya" not in rows


def test_boundless_cards_match_a_catalog_of_only_legacy_short_ids():
    html, cards = boundless_live_cards()
    payload = boundless_catalog_for([c for c in cards if c["sku"] == "deepseek/deepseek-v4-flash-0731"])
    payload["data"][0]["id"] = "dsv4"
    prices, rows = boundless.discover(payload, html)
    assert set(prices) == {"deepseek/deepseek-v4-flash-0731"}
    assert rows["deepseek/deepseek-v4-flash-0731"]["upstream_id"] == "dsv4"


def test_boundless_two_served_priced_cards_for_one_model_fail_closed():
    html, cards = boundless_live_cards()
    twin = copy.deepcopy(next(c for c in cards if c["sku"] == "z-ai/glm-5.3"))
    twin["sku"] = "glm-5.3"
    with pytest.raises(RuntimeError, match="duplicate canonical model ID among served rate cards"):
        boundless.discover(boundless_catalog_for(cards), boundless_html(*cards, twin))


@pytest.mark.parametrize("ids", [("glm-5.3-legacy", "z-ai/glm-5.3-legacy"), ("dsv4", "deepseek-v4-flash-0731")])
def test_boundless_alias_ids_without_a_card_naming_one_fail_closed(ids):
    # Neither ID is the identifier of a served rate card: no basis to choose.
    html, cards = boundless_live_cards()
    payload = boundless_catalog_for(cards)
    payload["data"] = [row for row in payload["data"] if row["id"] != "deepseek/deepseek-v4-flash-0731"]
    payload["data"] += [{"id": native, "context_length": 1048576} for native in ids]
    with pytest.raises(RuntimeError, match="duplicate canonical model ID"):
        boundless.discover(payload, html)


@pytest.mark.parametrize("previous_upstream,probed", [("mimo-v2.6-pro", True), ("xiaomi/mimo-v2.6-pro", False)])
def test_boundless_changed_upstream_id_is_canaried_again(monkeypatch, tmp_path, previous_upstream, probed):
    # Picking the rate card's identifier over a legacy alias can move an
    # existing route to a new upstream ID; it must pass a fresh canary.
    manifest = tmp_path / "boundless.json"
    manifest.write_text(json.dumps({"provider": "boundless", "models": [{
        "id": "xiaomi/mimo-v2.6-pro", "upstream_id": previous_upstream, "model_type": "chat", "routable": True,
    }]}))
    monkeypatch.setattr(boundless, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(boundless, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(boundless, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("BOUNDLESS_API_KEY", "fake-key")
    monkeypatch.setattr(boundless, "fetch_json", lambda *_a, **_kw: boundless_catalog("xiaomi/mimo-v2.6-pro"))
    monkeypatch.setattr(boundless, "fetch_html", lambda *_a, **_kw: boundless_html(boundless_product("xiaomi/mimo-v2.6-pro")))
    calls = []
    monkeypatch.setattr(boundless, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    boundless.fetch()
    row = boundless._DISCOVERED_ROWS["xiaomi/mimo-v2.6-pro"]
    assert [call["model"] for call in calls] == (["xiaomi/mimo-v2.6-pro"] if probed else [])
    assert row.get("routable_reason") == ("provider-canary-failed" if probed else None)


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


@pytest.mark.parametrize("healthy", [False, True])
def test_zro_rejected_minimax_requires_fresh_success_to_recover(monkeypatch, tmp_path, healthy):
    model = "minimax/minimax-m3.1-flash"
    native = "minimax-m3.1-flash"
    manifest = tmp_path / "zro.json"
    manifest.write_text(json.dumps({"provider": "zro", "models": [{
        "id": model, "upstream_id": native, "model_type": "chat",
        "routable": False, "routable_reason": "provider-canary-failed",
    }]}))
    monkeypatch.setattr(zro, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(zro, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(zro, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("ZRO_API_KEY", "test-key")
    monkeypatch.setattr(zro, "fetch_json", lambda *_a, **_kw: zro_catalog(native))
    monkeypatch.setattr(zro, "fetch_html", lambda *_a, **_kw: zro_html(native))
    calls = []
    monkeypatch.setattr(zro, "probe_openai_chat", lambda **kw: calls.append(kw) or healthy)
    zro.write_provider_manifest(zro.fetch())
    row = json.loads(manifest.read_text())["models"][0]
    assert calls[0]["model"] == native
    assert calls[0]["require_usage"] is True
    assert row["routable"] is healthy
    assert row.get("routable_reason") == (None if healthy else "provider-canary-failed")


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
        # Zro serves the bare ID. Boundless serves the legacy short ID and the
        # qualified one its rate card names (z-ai/glm-5.3-flash since
        # 2026-11); discovery prefers the card's, which passed the live PONG
        # canary in refresh run 42372150892. Either way it is this model.
        if slug == "zro":
            assert endpoint.upstream_id == "glm-5.3-flash"
        else:
            assert endpoint.upstream_id in {"glm-5.3-flash", "z-ai/glm-5.3-flash"}
            assert module.canonical_model_id(endpoint.upstream_id) == "z-ai/glm-5.3-flash"
        assert endpoint.usage_type == "Credits"
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


def test_boundless_legacy_short_ids_do_not_read_as_unpublished_models(monkeypatch, tmp_path):
    # Snapshot CI 42372491661: once the manifest routed z-ai/glm-5.3-flash
    # through its rate-card ID, the API's legacy short ID glm-5.3-flash was
    # reported as an unpublished model. A short ID for a routed model is the
    # alias discovery collapses; a short ID for an unknown model still warns.
    from scripts import check_price_coverage as coverage

    manifest_dir = tmp_path / "provider_models"
    manifest_dir.mkdir()
    (manifest_dir / "boundless.json").write_text(json.dumps({"models": [
        {"id": "z-ai/glm-5.3-flash", "upstream_id": "z-ai/glm-5.3-flash", "routable": True},
    ]}))
    monkeypatch.setattr(coverage, "MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(coverage, "_DISCOVERABLE_MANIFEST_PROVIDERS", (
        ("boundless", boundless.URL, ("BOUNDLESS_API_KEY",), boundless.canonical_model_id),
    ))

    def audit(*ids):
        return coverage._model_discovery_audit(
            fetch_text=lambda _url: "",
            fetch_json=lambda url, _env: {"data": [{"id": i} for i in ids]} if url == boundless.URL else {"data": []},
            published_model_ids=set(),
        )

    warnings, info = audit("glm-5.3-flash", "z-ai/glm-5.3-flash")
    assert not [w for w in warnings if w.startswith("boundless:")]
    assert any(line.startswith("boundless: model discovery matched catalog") for line in info)
    warnings, _ = audit("glm-5.3-flash", "z-ai/glm-5.3-flash", "glm-9-preview")
    assert any("boundless:" in w and "glm-9-preview" in w for w in warnings)


def test_boundless_gemma_short_alias_collapses_onto_its_card():
    html, cards = boundless_live_cards()
    prices, rows = boundless.discover(boundless_catalog_for(cards, ("gemma4-31b",)), html)
    assert rows["google/gemma-4-31b-it"]["upstream_id"] == "google/gemma-4-31b-it"
    assert "boundless/gemma4-31b" not in rows
