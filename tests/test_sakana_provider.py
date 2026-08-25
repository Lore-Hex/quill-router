from __future__ import annotations

import json
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, PriceTier
from scripts.pricing.providers import sakana
from trusted_router import catalog_ingest
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
<h4>Usage field details</h4>
<table>
  <tbody>
    <tr><td>input_tokens</td><td>Tokens from the user input sent to the first model.</td></tr>
    <tr><td>orchestration_input_tokens</td><td>Sum of all input tokens used for orchestration.</td></tr>
    <tr><td>orchestration_output_tokens</td><td>Output tokens from the orchestration.</td></tr>
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

    with pytest.raises(RuntimeError, match="orchestration usage contract changed"):
        sakana._parse_pricing(
            SAKANA_PRICING.replace(
                "Sum of all input tokens used for orchestration.",
                "Orchestration details unavailable.",
            )
        )


def test_sakana_live_fugu_pricing_drift_fails_refresh_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = SAKANA_PRICING.replace(
        "Sum of all input tokens used for orchestration.",
        "Orchestration details unavailable.",
    )
    monkeypatch.setattr(sakana, "fetch_html", lambda _url: drifted)

    with pytest.raises(RuntimeError, match="orchestration usage contract changed"):
        sakana._load_prices()


def test_sakana_pricing_keeps_namazu_available_after_fugu_retirement() -> None:
    soup = BeautifulSoup(SAKANA_PRICING, "html.parser")
    fugu_heading = next(
        node for node in soup.find_all("h4") if node.get_text(strip=True) == "Fugu Ultra"
    )
    usage_heading = next(
        node
        for node in soup.find_all("h4")
        if node.get_text(strip=True) == "Usage field details"
    )
    fugu_heading.find_next_sibling("p").decompose()
    fugu_heading.find_next_sibling("table").decompose()
    fugu_heading.decompose()
    usage_heading.find_next_sibling("table").decompose()
    usage_heading.decompose()

    assert sakana._parse_pricing(str(soup)) == {
        "sakana-ai/sakana-namazu-v1.0": ModelPrice(
            prompt_micro_per_m=950_000,
            completion_micro_per_m=4_000_000,
            prompt_cached_micro_per_m=150_000,
        )
    }


def test_sakana_routes_are_prepaid_only_and_use_the_operator_secret() -> None:
    assert "sakana" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert PROVIDERS["sakana"].supports_prepaid is True
    assert PROVIDERS["sakana"].supports_byok is False
    assert PROVIDERS["sakana"].provider_policy_url == "https://console.sakana.ai/privacy-policy"
    assert default_provider_secret_ref("sakana") == "env://SAKANA_API_KEY"

    fugu = endpoints_for_model("sakana-ai/fugu-ultra-v1.1")
    namazu = endpoints_for_model("sakana-ai/sakana-namazu-v1.0")
    direct_fugu = [endpoint for endpoint in fugu if endpoint.provider == "sakana"]
    assert len(direct_fugu) == 1
    assert direct_fugu[0].usage_type == "Credits"
    assert direct_fugu[0].prompt_price_microdollars_per_million_tokens == 5_000_000
    assert direct_fugu[0].completion_price_microdollars_per_million_tokens == 30_000_000
    assert [
        (
            tier.max_prompt_tokens,
            tier.prompt_price_microdollars_per_million_tokens,
            tier.completion_price_microdollars_per_million_tokens,
            tier.prompt_cached_price_microdollars_per_million_tokens,
        )
        for tier in direct_fugu[0].price_tiers
    ] == [
        (272_000, 5_000_000, 30_000_000, 500_000),
        (None, 10_000_000, 45_000_000, 1_000_000),
    ]
    assert not any(endpoint.provider == "sakana" for endpoint in namazu)


def test_sakana_declares_operator_holds_in_shared_fetcher() -> None:
    assert sakana.CATALOG.spec.operator_hold_reasons == {
        sakana.SAKANA_NAMAZU_MODEL_ID: sakana.SAKANA_NAMAZU_ROUTE_HOLD_REASON,
    }


@pytest.mark.parametrize(
    ("model_id", "upstream_id"),
    [(sakana.SAKANA_NAMAZU_MODEL_ID, "sakana-namazu-v1.0")],
)
def test_sakana_routes_are_code_held_even_if_a_manifest_route_is_enabled(
    model_id: str,
    upstream_id: str,
) -> None:
    template = next(
        endpoint
        for endpoint in endpoints_for_model("deepseek/deepseek-v4-flash")
        if endpoint.usage_type == "Credits"
    )
    held = replace(
        template,
        id=f"{model_id}@sakana/test-enabled",
        model_id=model_id,
        provider="sakana",
        upstream_id=upstream_id,
    )

    assert catalog_ingest._filter_unserved_provider_endpoints(
        {held.id: held},
        explicit_model_ids=frozenset(),
    ) == {}


def test_sakana_manifest_keeps_region_restricted_namazu_visible_but_dark() -> None:
    manifest = json.loads(sakana.MANIFEST_PATH.read_text(encoding="utf-8"))
    namazu = next(
        row
        for row in manifest["models"]
        if row["id"] == sakana.SAKANA_NAMAZU_MODEL_ID
    )
    assert namazu["routable"] is False
    assert namazu["routable_reason"] == sakana.SAKANA_NAMAZU_ROUTE_HOLD_REASON


def test_sakana_manifest_publishes_direct_fugu_with_exact_tiered_pricing() -> None:
    manifest = json.loads(sakana.MANIFEST_PATH.read_text(encoding="utf-8"))
    fugu = next(
        row
        for row in manifest["models"]
        if row["id"] == sakana.SAKANA_FUGU_MODEL_ID
    )

    assert fugu["routable"] is True
    assert "routable_reason" not in fugu
    assert fugu["input_token_price_per_m"] == 5_000_000
    assert fugu["output_token_price_per_m"] == 30_000_000
    assert fugu["cached_input_token_price_per_m"] == 500_000
    assert fugu["price_tiers"] == [
        {
            "max_prompt_tokens": 272_000,
            "input_token_price_per_m": 5_000_000,
            "output_token_price_per_m": 30_000_000,
            "cached_input_token_price_per_m": 500_000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 10_000_000,
            "output_token_price_per_m": 45_000_000,
            "cached_input_token_price_per_m": 1_000_000,
        },
    ]
