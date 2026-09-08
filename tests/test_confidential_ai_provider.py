"""Provider-owned availability and exact prices gate Confidential AI routes."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import _direct_openai, confidential_ai
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_ingest import _supplemental_provider_models_and_endpoints
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.pricing import _customer_price
from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
from trusted_router.services.inference_errors import default_provider_secret_ref

FLASH = "deepseek/deepseek-v4-flash-0731"
M3 = "minimax/minimax-m3"
NATIVE_FLASH = "deepseek-ai/DeepSeek-V4-Flash-0731"
PRICES = """
<table><tr><th>Model</th><th>Input (per 1M tokens)</th>
<th>Input Cached (per 1M tokens)</th><th>Output (per 1M tokens)</th></tr>
<tr><td>DeepSeek V4-Flash</td><td>$0.20</td><td>$0.018</td><td>$0.40</td></tr>
<tr><td>Kimi K3</td><td>$3.00</td><td>$0.30</td><td>$15.00</td></tr>
</table><table><tr><th>GPU</th><th>Per GPU-Hour</th></tr>
<tr><td>H100</td><td>$3.25</td></tr></table>
"""
ROWS = [{"id": NATIVE_FLASH}, {"id": "MiniMaxAI/MiniMax-M3-MXFP8"}]


def test_exact_input_cached_output_units_and_dated_alias() -> None:
    prices = confidential_ai._published_prices(PRICES)
    assert prices[FLASH] == ModelPrice(200_000, 400_000, prompt_cached_micro_per_m=18_000)
    assert "deepseek/deepseek-v4-flash-9999" not in prices
    assert M3 not in prices
    assert "h100" not in prices


def test_reordered_columns_do_not_swap_cached_and_output_prices() -> None:
    soup = BeautifulSoup(PRICES, "html.parser")
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        tr.insert(0, cells[-1].extract())
    assert confidential_ai._published_prices(str(soup)) == confidential_ai._published_prices(PRICES)


@pytest.mark.parametrize("bad", ["$NaN", "$Infinity", "$-1", "$oops", "$0.0000001", "$0.21"])
def test_invalid_or_excessive_cached_prices_fail_closed(bad: str) -> None:
    with pytest.raises(RuntimeError, match="confidential-ai"):
        confidential_ai._published_prices(PRICES.replace("$0.018", bad))


@pytest.mark.parametrize(
    "html",
    [
        "",
        PRICES.replace("Input Cached", "Unknown"),
        PRICES.replace("<td>$0.40</td>", ""),
        PRICES + PRICES.replace("$0.40", "$0.80"),
        PRICES.replace("$0.40", "$0.00"),
    ],
)
def test_missing_malformed_or_conflicting_tables_fail_closed(html: str) -> None:
    with pytest.raises(RuntimeError, match="confidential-ai"):
        confidential_ai._published_prices(html)


def _catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    healthy: bool = True,
    rows=None,
    html: str = PRICES,
) -> DirectOpenAIProvider:
    monkeypatch.setenv("CONFIDENTIAL_AI_API_KEY", "test-only-key")
    monkeypatch.setattr(confidential_ai, "fetch_html", lambda url: html)

    def fetch(url, *, extra_headers):
        assert url == confidential_ai.URL
        assert extra_headers == {"Authorization": "Bearer test-only-key"}
        return {"data": ROWS if rows is None else rows}

    monkeypatch.setattr(_direct_openai, "fetch_json", fetch)

    def probe(**kwargs):
        assert kwargs["model"] != "MiniMaxAI/MiniMax-M3-MXFP8" or "MiniMax M3" in html
        assert kwargs["base_url"] == confidential_ai.BASE_URL
        return healthy

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    return DirectOpenAIProvider(
        replace(confidential_ai.CATALOG.spec), manifest_path=tmp_path / "confidential-ai.json"
    )


def test_discovery_publishes_only_available_priced_canary_and_preserves_pending(
    tmp_path, monkeypatch
):
    catalog = _catalog(tmp_path, monkeypatch)
    result = catalog.fetch()
    assert set(result.prices) == {FLASH}
    assert catalog.upstream_id_map[FLASH] == NATIVE_FLASH
    catalog.write_provider_manifest(result)
    manifest = json.loads(catalog.manifest_path.read_text())
    rows = {row["id"]: row for row in manifest["models"]}
    assert rows[FLASH].get("routable") is not False
    assert rows[FLASH]["cached_input_token_price_per_m"] == 18_000
    assert rows[M3]["routable"] is False
    assert rows[M3]["routable_reason"] == "awaiting-price"
    assert "moonshotai/kimi-k3" not in rows  # Public waitlist is not availability.


def test_failed_canary_keeps_route_dark(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path, monkeypatch, healthy=False)
    catalog.write_provider_manifest(catalog.fetch())
    rows = json.loads(catalog.manifest_path.read_text())["models"]
    assert all(row["routable"] is False for row in rows)


def test_new_priced_model_is_automatically_discovered(tmp_path, monkeypatch):
    html = PRICES.replace("Kimi K3", "GLM 5.3")
    catalog = _catalog(tmp_path, monkeypatch, rows=[{"id": "zai-org/GLM-5.3"}], html=html)
    assert set(catalog.fetch().prices) == {"z-ai/glm-5.3"}


def test_m3_becomes_eligible_only_when_provider_publishes_price(tmp_path, monkeypatch):
    html = PRICES.replace("Kimi K3", "MiniMax M3")
    catalog = _catalog(tmp_path, monkeypatch, html=html)
    assert set(catalog.fetch().prices) == {FLASH, M3}


def test_missing_key_and_unpriced_only_catalog_fail_closed(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path, monkeypatch, rows=[ROWS[1]])
    with pytest.raises(RuntimeError, match="no priced chat"):
        catalog.fetch()
    monkeypatch.delenv("CONFIDENTIAL_AI_API_KEY")
    with pytest.raises(RuntimeError, match="required for discovery"):
        catalog.fetch()


def test_provider_privacy_and_discovery_contracts() -> None:
    provider = PROVIDERS[confidential_ai.SLUG]
    assert provider.provider_zero_data_retention and not provider.stores_content
    assert provider.provider_e2ee is False
    assert provider.supports_prepaid and not provider.supports_byok
    assert confidential_ai.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert confidential_ai.SLUG in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert "confidential_ai" in PROVIDER_SLUGS
    assert confidential_ai.SLUG in {row[0] for row in _DISCOVERABLE_MANIFEST_PROVIDERS}
    assert default_provider_secret_ref(confidential_ai.SLUG) == "env://CONFIDENTIAL_AI_API_KEY"


def test_live_manifest_builds_credits_route_with_cache_billing_and_zdr_not_e2e() -> None:
    _models, endpoints = _supplemental_provider_models_and_endpoints()
    matches = [e for e in endpoints.values() if e.provider == confidential_ai.SLUG]
    assert len(matches) == 1
    endpoint = matches[0]
    assert endpoint.model_id == FLASH
    assert endpoint.upstream_id == NATIVE_FLASH
    assert endpoint.usage_type == "Credits"
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(200_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(400_000)
    assert endpoint.price_tiers[
        0
    ].prompt_cached_price_microdollars_per_million_tokens == _customer_price(18_000)
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)


def test_hourly_refresh_has_narrow_secret_binding() -> None:
    root = Path(__file__).resolve().parents[1]
    resource_name = "trustedrouter-confidential-ai-api-key"
    assert (
        f"CONFIDENTIAL_AI_API_KEY:{resource_name}"
        in (root / ".github/workflows/refresh-prices.yml").read_text()
    )
    script = (root / "scripts/deploy/secrets.sh").read_text()
    assert f'grant_tr_deploy_secret_access "{resource_name}"' in script
    assert f'ensure_secret_from_env_file "CONFIDENTIAL_AI_API_KEY" "{resource_name}"' in script
