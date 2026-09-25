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
                                  PRICES.replace("EUR 0.05", "EUR 5"), "<html>unavailable</html>"])
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
