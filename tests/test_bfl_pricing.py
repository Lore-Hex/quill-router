from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scripts.pricing.providers import bfl


def calculator_html(cards: list[dict[str, Any]]) -> str:
    payload = '53:' + json.dumps(["$", "div", None, {"data": {"pricingCards": cards}}])
    return '<script>self.__next_f.push(' + json.dumps([1, payload + '\n']) + ')</script>'


def test_calculator_prices_are_first_megapixel_credits_not_dollars() -> None:
    html = calculator_html([
        {"apiEndpoint": "/v1/flux-2-klein-4b", "firstMpPrice": 1.4, "mpPrice": 0.1},
        {"apiEndpoint": "/v1/flux-2-flex", "firstMpPrice": 5, "mpPrice": 5},
        {"apiEndpoint": "/v1/flux-3-image", "firstMpPrice": 999},
    ])
    assert bfl._parse_calculator_pricing(html) == {
        "black-forest-labs/flux-2-klein-4b": 14_000,
        "black-forest-labs/flux-2-flex": 50_000,
    }


@pytest.mark.parametrize("value", [None, True, 0, -1, "NaN", "Infinity", "bad", 0.000001])
def test_calculator_rejects_invalid_prices(value: Any) -> None:
    with pytest.raises(ValueError, match="BFL calculator"):
        bfl._parse_calculator_pricing(calculator_html([
            {"apiEndpoint": "/v1/flux-2-pro", "firstMpPrice": value},
        ]))


def test_calculator_rejects_conflicting_prices() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        bfl._parse_calculator_pricing(calculator_html([
            {"apiEndpoint": "/v1/flux-2-pro", "firstMpPrice": 3},
            {"apiEndpoint": "/v1/flux-2-pro", "firstMpPrice": 4},
        ]))


def test_fetch_uses_calculator_when_documentation_price_cells_are_empty(monkeypatch) -> None:
    prices = [1.4, 1.5, 3, 7, 5]
    cards = [
        {"apiEndpoint": f"/v1/{upstream}", "firstMpPrice": price}
        for (_, upstream), price in zip(bfl.MODELS.values(), prices, strict=True)
    ]
    seen: list[str] = []

    def fetch_html(url: str) -> str:
        seen.append(url)
        if url == bfl.PRICING_URL:
            return "<table><tr><td>FLUX.2 [pro]</td><td>from </td></tr></table>"
        return calculator_html(cards)

    def get(_self: httpx.Client, url: str, **_kwargs: Any) -> httpx.Response:
        body = {"paths": {card["apiEndpoint"]: {} for card in cards}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setenv("BFL_API_KEY", "test-only")
    monkeypatch.setattr(bfl, "fetch_html", fetch_html)
    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(bfl, "_DISCOVERED_ROWS", {})
    result = bfl.fetch()
    assert seen == [bfl.PRICING_URL, "https://bfl.ai/pricing"]
    assert result.fetched_url == "https://bfl.ai/pricing"
    assert len(result.prices) == 5
    assert bfl._DISCOVERED_ROWS["black-forest-labs/flux-2-pro"][
        "fixed_output_price_microdollars"
    ] == {"1k": 30_000}
