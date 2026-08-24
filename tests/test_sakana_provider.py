from __future__ import annotations

import pytest

from scripts.pricing.base import ModelPrice, PriceTier
from scripts.pricing.providers import sakana
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PROVIDERS,
    endpoints_for_model,
)
from trusted_router.services.inference_errors import default_provider_secret_ref

SAKANA_PRICING = """
<h4>Fugu Ultra</h4>
<p>Fixed pricing for <code>fugu-ultra-v1.1</code> per 1M tokens.</p>
<table>
  <thead><tr><th>Token type</th><th>Standard price</th><th>Context &gt; 272K</th></tr></thead>
  <tbody>
    <tr><td>Input</td><td>$5</td><td>$10</td></tr>
    <tr><td>Output</td><td>$30</td><td>$45</td></tr>
    <tr><td>Cached input</td><td>$0.50</td><td>$1.00</td></tr>
  </tbody>
</table>
<h3>sakana-namazu-v1.0</h3>
<p>Token pricing per 1M tokens.</p>
<table>
  <thead><tr><th>Token type</th><th>Price</th></tr></thead>
  <tbody>
    <tr><td>Input</td><td>$0.95</td></tr>
    <tr><td>Output</td><td>$4.00</td></tr>
    <tr><td>Cached input</td><td>$0.15</td></tr>
  </tbody>
</table>
"""


def test_sakana_publishes_only_exactly_billable_models() -> None:
    assert sakana.CATALOG.model_id("fugu-ultra-v1.1") == "sakana-ai/fugu-ultra-v1.1"
    assert (
        sakana.CATALOG.model_id("sakana-namazu-v1.0")
        == "sakana-ai/sakana-namazu-v1.0"
    )
    assert sakana._include({"id": "fugu-ultra-v1.1"}) is True
    assert sakana._include({"id": "sakana-namazu-v1.0"}) is True
    assert sakana._include({"id": "fugu"}) is False
    assert sakana._include({"id": "fugu-cyber"}) is False
    assert sakana._include({"id": "sakana-namazu"}) is False


def test_sakana_first_party_prices_are_exact_and_tiered() -> None:
    assert sakana._parse_pricing(SAKANA_PRICING) == {
        "sakana-ai/fugu-ultra-v1.1": ModelPrice(
            tiers=[
                PriceTier(
                    max_prompt_tokens=272_000,
                    prompt_micro_per_m=5_000_000,
                    completion_micro_per_m=30_000_000,
                    prompt_cached_micro_per_m=500_000,
                ),
                PriceTier(
                    max_prompt_tokens=None,
                    prompt_micro_per_m=10_000_000,
                    completion_micro_per_m=45_000_000,
                    prompt_cached_micro_per_m=1_000_000,
                ),
            ]
        ),
        "sakana-ai/sakana-namazu-v1.0": ModelPrice(
            prompt_micro_per_m=950_000,
            completion_micro_per_m=4_000_000,
            prompt_cached_micro_per_m=150_000,
        ),
    }


def test_sakana_pricing_parser_fails_closed_on_layout_drift() -> None:
    with pytest.raises(RuntimeError, match="context tier changed"):
        sakana._parse_pricing(SAKANA_PRICING.replace("Context &gt; 272K", "Long context"))


def test_sakana_routes_are_prepaid_only_and_use_the_operator_secret() -> None:
    assert "sakana" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert PROVIDERS["sakana"].supports_prepaid is True
    assert PROVIDERS["sakana"].supports_byok is False
    assert default_provider_secret_ref("sakana") == "env://SAKANA_API_KEY"

    fugu = endpoints_for_model("sakana-ai/fugu-ultra-v1.1")
    namazu = endpoints_for_model("sakana-ai/sakana-namazu-v1.0")
    assert any(endpoint.provider == "sakana" for endpoint in fugu)
    assert any(endpoint.provider == "sakana" for endpoint in namazu)
