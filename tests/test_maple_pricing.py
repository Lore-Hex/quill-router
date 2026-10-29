import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from scripts.pricing.providers import maple
from trusted_router.provider_manifest_policy import (
    EXPIRED_PROVIDER_MANIFEST,
    provider_manifest_valid_until,
)

PRICES = """<p>USD equivalent per 1 million tokens.</p><table>
<thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>
<tr><th scope="row">DeepSeek V4.1 Flash</th><td>$0.01</td><td>$0.001</td><td>$0.02</td></tr>
<tr><th scope="row">DeepSeek V4.1 Flash Uncensored</th><td>$0.65</td><td>$0.13</td><td>$1.45</td></tr>
</tbody></table>"""
CAPABILITIES = """<table><thead><tr><th>Model</th><th>Context tokens</th><th>Image input</th></tr></thead>
<tbody>
<tr><td>DeepSeek V4.1 Flash Uncensored <code>deepseek-v4-1-flash-1017-uncensored</code></td>
<td>1,048,576</td><td>Yes</td></tr></tbody></table>"""


def test_exact_uncensored_prices_never_use_stock_or_plan_prices():
    price = maple.parse_prices(PRICES)
    assert price.prompt_micro_per_m == 650_000
    assert price.tiers[0].prompt_cached_micro_per_m == 130_000
    assert price.completion_micro_per_m == 1_450_000


@pytest.mark.parametrize(("before", "after"), [
    ("USD equivalent per 1 million tokens.", "EUR equivalent per 1 million tokens."),
    ("$0.65", "$0"), ("$1.45", "$0"), ("$0.13", "$0.66"),
    ("$0.13", "$-0.13"), ("$0.13", "$0.1234567"), ("$0.13", "$0.13 per token"),
    ("DeepSeek V4.1 Flash Uncensored", "DeepSeek V4.2 Flash Uncensored"),
])
def test_bad_units_or_rates_fail_closed(before, after):
    with pytest.raises(RuntimeError):
        maple.parse_prices(PRICES.replace(before, after))


def test_duplicate_model_price_is_ambiguous_even_when_equal():
    with pytest.raises(RuntimeError):
        maple.parse_prices(PRICES + PRICES)


def test_exact_native_capability_and_context():
    assert maple.parse_context(CAPABILITIES) == 1_048_576
    for changed in [
        CAPABILITIES.replace(maple.NATIVE_ID, "deepseek-v4-1-flash"),
        CAPABILITIES.replace("1,048,576", "2,097,152"),
        CAPABILITIES.replace("1,048,576", "unlimited"),
        CAPABILITIES + CAPABILITIES,
    ]:
        with pytest.raises(RuntimeError):
            maple.parse_context(changed)


def test_fetch_writes_held_text_only_route_without_inventing_fees(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    result = maple.fetch()
    maple.write_provider_manifest(result)
    manifest = json.loads(maple.MANIFEST_PATH.read_text())
    assert len(manifest["models"]) == 1
    row = manifest["models"][0]
    assert row["id"] == maple.MODEL_ID
    assert row["upstream_id"] == maple.NATIVE_ID
    assert row["routable"] is False
    assert row["routable_reason"] == "attested-enclave-rollout-pending"
    assert row["input_modalities"] == ["text"]
    assert row["supports_tools"] is False
    assert row["cached_input_token_price_per_m"] == 130_000
    assert not any("fixed" in key or "minimum" in key or "rounding" in key for key in row)


def test_failed_refresh_cannot_reuse_prior_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    result = maple.fetch()
    pages[maple.MODEL_DOCS_URL] = "temporarily unavailable"
    with pytest.raises(RuntimeError):
        maple.fetch()
    with pytest.raises(RuntimeError, match="fresh reviewed"):
        maple.write_provider_manifest(result)
    assert not maple.MANIFEST_PATH.exists()


def test_price_refresh_does_not_clear_failed_encrypted_canary(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    monkeypatch.setattr(maple, "_PENDING_ENCLAVE_MODELS", frozenset())
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.MANIFEST_PATH.write_text(json.dumps({"provider": "maple", "models": [{
        "id": maple.MODEL_ID, "routable": False, "routable_reason": "provider-canary-failed",
    }]}))
    maple.write_provider_manifest(maple.fetch())
    row = json.loads(maple.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"


def test_maple_manifest_prices_expire_and_pending_routes_stay_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.write_provider_manifest(maple.fetch())
    manifest = json.loads(maple.MANIFEST_PATH.read_text())
    assert provider_manifest_valid_until("maple", manifest) == EXPIRED_PROVIDER_MANIFEST
    # Even after a future reviewed promotion, the last good prices have a deadline.
    manifest["models"][0]["routable"] = True
    generated = datetime(2026, 10, 28, tzinfo=UTC)
    manifest["generated_at"] = generated.isoformat()
    assert provider_manifest_valid_until("maple", manifest) == generated + timedelta(days=14)
    manifest["generated_at"] = "invalid"
    assert provider_manifest_valid_until("maple", manifest) == EXPIRED_PROVIDER_MANIFEST


@pytest.mark.parametrize("uncached,cached,output", [
    (0, 0, 0), (1, 0, 1), (36, 0, 4), (81, 12_288, 19),
    (1_000_000, 0, 100_000), (0, 1_000_000, 100_000),
])
def test_maple_bills_actual_cache_meters_without_provider_credit_rounding(uncached, cached, output):
    from trusted_router.authorize_candidates import endpoint_cost_microdollars
    from trusted_router.catalog_data import ModelEndpoint, PriceTier

    price = maple.parse_prices(PRICES)
    endpoint = ModelEndpoint(
        id="maple-cache-oracle", model_id=maple.MODEL_ID,
        upstream_id=maple.NATIVE_ID, provider="maple", usage_type="Credits",
        price_tiers=tuple(PriceTier(t.max_prompt_tokens, t.prompt_micro_per_m,
                                   t.completion_micro_per_m, t.prompt_cached_micro_per_m)
                          for t in price.tiers),
    )
    # Independent public-tariff oracle; cache reads replace, never add to,
    # uncached prompt tokens. Preserve the ledger's microdollar rounding.
    expected = sum(int((Decimal(count) * Decimal(rate) / 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                   for count, rate in zip((uncached, cached, output), (650_000, 130_000, 1_450_000), strict=True))
    assert endpoint_cost_microdollars(endpoint, uncached, output, cache_read_tokens=cached) == expected
