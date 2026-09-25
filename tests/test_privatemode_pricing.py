import json
from decimal import Decimal

import pytest

from scripts.pricing.currency import eur_microdollars_per_million, usd_per_eur
from scripts.pricing.providers import privatemode

PRICES = """<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
<tr><td>GLM-5.3</td><td>EUR 1.55</td><td>EUR 7.74</td><td>EUR 0.15</td></tr>
<tr><td>GLM-5.3-Flash</td><td>EUR 0.20</td><td>EUR 0.65</td><td>EUR 0.05</td></tr>
<tr><td>gpt-oss-120b</td><td>EUR 0.43</td><td>EUR 1.70</td><td>EUR 0.04</td></tr>
<tr><td>New unreviewed model</td><td>EUR 1</td><td>EUR 1</td><td>EUR 1</td></tr></table>"""


def test_prices_include_cached_rate_and_currency_reserve():
    prices = privatemode.parse_prices(PRICES, Decimal("1.20"))
    assert set(prices) == set(privatemode.UPSTREAM_ID_MAP)
    flash = prices["z-ai/glm-5.3-flash"]
    assert flash.prompt_micro_per_m == 252000
    assert flash.completion_micro_per_m == 819000
    assert flash.tiers[0].prompt_cached_micro_per_m == 63000


@pytest.mark.parametrize("html", [PRICES.replace("EUR", "$"), PRICES+PRICES,
                                  PRICES.replace("EUR 0.05", "EUR 5"),
                                  PRICES.replace("EUR 0.20", "EUR 0"),
                                  PRICES.replace("EUR 0.65", "EUR 0"),
                                  PRICES.replace("EUR 0.05", "EUR 0"), "<html>unavailable</html>"])
def test_prices_fail_closed(html):
    with pytest.raises(RuntimeError):
        privatemode.parse_prices(html, Decimal("1.2"))


@pytest.mark.parametrize("xml", ["<Cube/>", "<Cube currency='USD' rate='NaN'/>",
                                 "<Cube currency='USD' rate='-1'/>", "not xml",
                                 "<root><Cube currency='USD' rate='1'/><Cube currency='USD' rate='2'/></root>"])
def test_fx_requires_exact_finite_positive_rate(xml):
    with pytest.raises(RuntimeError):
        usd_per_eur(xml)


def test_fx_uses_xml_attributes_not_order_and_rounds_up():
    assert usd_per_eur("<Cube rate='1.2' currency='USD'/>") == Decimal("1.2")
    assert eur_microdollars_per_million(Decimal("0.000001"), Decimal("1.2")) == 2


def test_discovery_never_sends_plaintext_inference(monkeypatch, httpx_mock, tmp_path):
    monkeypatch.setenv("PRIVATEMODE_API_KEY", "synthetic-key")
    httpx_mock.add_response(url=privatemode.CATALOG_URL, json={"data": [
        {"id": "gpt-oss-120b"}, {"id": "glm-5.2"}, {"id": "glm-latest"}, {"id": "new-model"},
    ]})
    monkeypatch.setattr(privatemode, "fetch_html", lambda url: PRICES if url == privatemode.PRICING_URL
                        else "<Cube currency='USD' rate='1.2'/>")
    result = privatemode.fetch()
    assert set(result.prices) == {"openai/gpt-oss-120b"}
    requests = httpx_mock.get_requests()
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-key"
    assert privatemode._DISCOVERED["z-ai/glm-5.3"]["routable"] is False
    assert privatemode._DISCOVERED["openai/gpt-oss-120b"]["routable"] is True
    assert set(privatemode._DISCOVERED) == set(privatemode.UPSTREAM_ID_MAP)
    assert "glm-latest" not in privatemode.UPSTREAM_ID_MAP.values()
    monkeypatch.setattr(privatemode, "MANIFEST_PATH", tmp_path / "provider.json")
    privatemode.write_provider_manifest(result)
    assert "cached_input_token_price_per_m" in privatemode.MANIFEST_PATH.read_text()


def test_discovery_rejects_credential_redirect(monkeypatch, httpx_mock):
    monkeypatch.setenv("PRIVATEMODE_API_KEY", "synthetic-key")
    httpx_mock.add_response(url=privatemode.CATALOG_URL, status_code=307,
                            headers={"Location": "https://untrusted.invalid/models"})
    with pytest.raises(Exception, match="307"):
        privatemode.fetch()
    assert len(httpx_mock.get_requests()) == 1


def test_confidential_privatemode_has_only_reviewed_priced_credits_routes():
    from trusted_router import catalog

    assert catalog.PROVIDERS["privatemode"].provider_e2ee is True
    assert catalog.PROVIDERS["privatemode"].supports_byok is False
    for model_id, upstream_id in privatemode.UPSTREAM_ID_MAP.items():
        routes = [e for e in catalog.endpoints_for_model(model_id) if e.provider == "privatemode"]
        assert len(routes) == 1
        route = routes[0]
        assert route.usage_type == "Credits"
        assert route.upstream_id == upstream_id
        assert catalog.endpoint_privacy_tier(route) == catalog.PRIVACY_TIER_CONFIDENTIAL
        assert route.prompt_price_microdollars_per_million_tokens > 0
        assert route.completion_price_microdollars_per_million_tokens > 0


@pytest.mark.parametrize("reason", ["provider-canary-failed", "operator-review", "attested-enclave-rollout-pending"])
def test_metadata_refresh_preserves_safety_holds(monkeypatch, tmp_path, reason):
    path = tmp_path / "provider.json"
    row = {"id": "z-ai/glm-5.3", "routable": False, "routable_reason": reason}
    path.write_text(json.dumps({"provider": "privatemode", "models": [row]}))
    monkeypatch.setattr(privatemode, "MANIFEST_PATH", path)
    monkeypatch.setattr(privatemode, "_DISCOVERED", {row["id"]: {"id": row["id"], "routable": True}})
    result = privatemode.ProviderPricingResult(slug="privatemode", source="api",
                                              prices=privatemode.parse_prices(PRICES, Decimal("1.2")))
    privatemode.write_provider_manifest(result)
    saved = json.loads(path.read_text())["models"][0]
    assert saved["routable"] is False
    assert saved["routable_reason"] == reason


@pytest.mark.parametrize("missing", ["listing", "price"])
def test_unavailable_model_recovers_after_metadata_returns(monkeypatch, tmp_path, httpx_mock, missing):
    monkeypatch.setenv("PRIVATEMODE_API_KEY", "synthetic-key")
    path = tmp_path / "provider.json"
    monkeypatch.setattr(privatemode, "MANIFEST_PATH", path)
    for unavailable in (True, False):
        httpx_mock.add_response(url=privatemode.CATALOG_URL, json={"data": [
            {"id": native} for native in privatemode.MODELS
            if not (unavailable and missing == "listing" and native == "glm-5.3")
        ]})
        prices = PRICES.replace("<td>GLM-5.3</td>", "<td>Not available</td>") if unavailable and missing == "price" else PRICES
        monkeypatch.setattr(privatemode, "fetch_html", lambda url, prices=prices: prices if url == privatemode.PRICING_URL
                            else "<Cube currency='USD' rate='1.2'/>")
        privatemode.write_provider_manifest(privatemode.fetch())
        row = next(row for row in json.loads(path.read_text())["models"] if row["id"] == "z-ai/glm-5.3")
        assert row["routable"] is (not unavailable)
        if not unavailable:
            assert "routable_reason" not in row


def test_relisted_model_without_price_stays_unroutable(monkeypatch, tmp_path, httpx_mock):
    monkeypatch.setenv("PRIVATEMODE_API_KEY", "synthetic-key")
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"provider": "privatemode", "models": [
        {"id": "z-ai/glm-5.3", "routable": False, "routable_reason": "delisted-upstream",
         "input_token_price_per_m": 1, "output_token_price_per_m": 1},
    ]}))
    monkeypatch.setattr(privatemode, "MANIFEST_PATH", path)
    httpx_mock.add_response(url=privatemode.CATALOG_URL, json={"data": [
        {"id": native} for native in privatemode.MODELS
    ]})
    prices = PRICES.replace("<td>GLM-5.3</td>", "<td>Not available</td>")
    monkeypatch.setattr(privatemode, "fetch_html", lambda url: prices if url == privatemode.PRICING_URL
                        else "<Cube currency='USD' rate='1.2'/>")
    privatemode.write_provider_manifest(privatemode.fetch())
    row = next(row for row in json.loads(path.read_text())["models"] if row["id"] == "z-ai/glm-5.3")
    assert row["routable"] is False
    assert row["routable_reason"] == "price-unavailable"
    assert "input_token_price_per_m" not in row
    assert "output_token_price_per_m" not in row
