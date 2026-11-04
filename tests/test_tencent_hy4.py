from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from scripts.pricing.providers import _direct_openai, tencent
from tests import catalog_vehicles
from trusted_router.provider_lifecycle import (
    ProviderPrice,
    provider_price_microdollars,
    provider_pricing_schedule,
)

FIXTURE = Path(__file__).parent / "fixtures/pricing/tencent_hy4_2026-11-04.html"
MODEL = "tencent/hy4"
MODELS_FIXTURE = FIXTURE.with_name("tencent_hy4_models_2026-11-04.html")


def test_hy4_capabilities_and_identity_do_not_replace_preview() -> None:
    metadata = tencent.parse_models(MODELS_FIXTURE.read_text())
    assert tencent.canonical_model_id("hy4") == MODEL
    assert tencent.canonical_model_id("hy4-preview") == "tencent/hy4-preview"
    assert metadata["hy4"]["input_modalities"] == ["text", "image"]
    assert metadata["hy4-preview"]["input_modalities"] == ["text"]
    assert metadata["hy4"]["context_length"] == 1_048_576
    assert metadata["hy4"]["max_output_tokens"] == 65_536
    assert metadata["hy4"]["supported_features"] == [
        "function-calling", "json-mode", "reasoning-effort", "prompt-cache",
    ]


def test_hy4_manifest_preserves_managed_canary_gate_and_privacy() -> None:
    from trusted_router.catalog import PROVIDERS, model_open_weights

    row = next(r for r in json.loads(tencent.MANIFEST_PATH.read_text())["models"] if r["id"] == MODEL)
    assert row["upstream_id"] == "hy4"
    managed = [e for e in catalog_vehicles.registry_endpoints().values()
               if e.model_id == MODEL and e.provider == "tencent" and e.usage_type == "Credits"]
    assert bool(managed) is (row.get("routable") is True)
    preview = catalog_vehicles.registry_models()["tencent/hy4-preview"]
    assert model_open_weights(preview)
    assert not model_open_weights(replace(preview, id=MODEL))
    provider = PROVIDERS["tencent"]
    assert provider.provider_zero_data_retention is not True
    assert provider.provider_confidential_compute is not True


@pytest.mark.parametrize("status,canary_ok", [("online", True), ("online", False), ("offline", True)])
def test_hy4_discovery_requires_account_availability_and_successful_canary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, canary_ok: bool,
) -> None:
    monkeypatch.setenv("TENCENT_API_KEY", "test-key")
    catalog = _direct_openai.DirectOpenAIProvider(
        replace(tencent.CATALOG.spec, expected_models=()), manifest_path=tmp_path / "tencent.json",
    )
    monkeypatch.setattr(tencent, "CATALOG", catalog)
    monkeypatch.setattr(tencent, "fetch_html", lambda url: (
        MODELS_FIXTURE.read_text() if url == tencent.MODELS_DOC_URL else FIXTURE.read_text()
    ))
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": [
        {"id": "hy4", "status": status}, {"id": "hy4-preview", "status": "online"},
    ]})
    probes: list[dict[str, Any]] = []

    def probe(**kwargs: Any) -> bool:
        probes.append(kwargs)
        return kwargs["model"] != "hy4" or canary_ok

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    result = tencent.fetch()
    if status == "online":
        assert catalog.discovered_rows[MODEL]["routable"] is canary_ok
        assert catalog.discovered_rows[MODEL]["input_modalities"] == ["text", "image"]
        assert result.prices[MODEL].completion_micro_per_m == 2_501_000
        assert [p["model"] for p in probes].count("hy4") == 1
    else:
        assert MODEL not in result.prices
        assert MODEL not in catalog.discovered_rows
        assert all(p["model"] != "hy4" for p in probes)
    assert all(p["base_url"] == tencent.BASE_URL and p["max_tokens"] == 256 for p in probes)
    assert all(p["require_usage"] is True and p["expected_content"] == "PONG" for p in probes)


def test_hy4_price_table_keeps_list_prices_and_preview_distinct() -> None:
    prices = tencent.parse_prices(FIXTURE.read_text())
    for name in ("hy4", "hy4 preview"):
        tier, = prices[name].tiers
        assert (tier.prompt_micro_per_m, tier.completion_micro_per_m,
                tier.prompt_cached_micro_per_m) == (834_000, 2_501_000, 42_000)
    assert prices["hy3"].prompt_micro_per_m == 132_000
    # The token-price profile cannot represent explicit cache-write charges.
    assert "kimi k3.1 pro" not in prices


@pytest.mark.parametrize("old,new", [
    ("50%", "40%"), ("December 31, 2026", "December 30, 2026"),
    ("Beijing Time", "UTC"), ("0.834", "0.900"),
    ("Cache Write - 5min", "Unknown Charge"),
    ("Input Tokens (USD / million tokens)", "Output Tokens (USD / million tokens)"),
])
def test_hy4_rejects_unreviewed_tariff_or_promotion(old: str, new: str) -> None:
    with pytest.raises(ValueError):
        tencent.parse_prices(FIXTURE.read_text().replace(old, new))


def test_hy4_requires_the_reviewed_promotion_notice() -> None:
    html = FIXTURE.read_text()
    with pytest.raises(ValueError):
        tencent.parse_prices(html[html.index('<div class="tse-tabs">'):])


def test_hy4_refresh_does_not_enable_unreviewed_scheduled_models() -> None:
    html = FIXTURE.read_text()
    rows = (
        '<tr><td>DeepSeek-V4.1-Flash-1017</td><td>-</td><td>OFF-PEAK</td>'
        '<td>0.15</td><td>0.6</td><td>0.003</td><td>-</td><td>-</td></tr>'
        '<tr><td>DeepSeek-V4.1-Flash-1017</td><td>-</td><td>PEAK</td>'
        '<td>0.3</td><td>1.2</td><td>0.006</td><td>-</td><td>-</td></tr>'
    )
    html = html.replace('  </table>', rows + '</table>')
    prices = tencent.parse_prices(html)
    assert "deepseek-v4.1-flash-1017" not in prices
    assert "hy4" in prices
    with pytest.raises(ValueError, match="new scheduled model"):
        tencent.parse_prices(html.replace('DeepSeek-V4.1-Flash-1017', 'unreviewed-model'))


@pytest.mark.parametrize("at,discounted", [
    ("2026-11-03T15:59:59Z", False),
    ("2026-11-03T16:00:00Z", True),
    ("2026-11-04T07:31:35Z", True),
    ("2026-12-31T15:59:59.999999Z", True),
    ("2026-12-31T16:00:00Z", False),
    ("2027-01-01T00:00:00Z", False),
])
def test_hy4_discount_uses_beijing_calendar_and_all_token_directions(
    at: str, discounted: bool,
) -> None:
    expected = ProviderPrice(417_000, 1_250_500, 21_000) if discounted else ProviderPrice(834_000, 2_501_000, 42_000)
    assert provider_price_microdollars("tencent", MODEL, at=at) == expected
    assert provider_price_microdollars("tencent", "tencent/hy4-preview", at=at) is None
    assert provider_price_microdollars("gmi", MODEL, at=at) is None
    schedule = provider_pricing_schedule("tencent", MODEL, at=at)
    assert schedule is not None
    assert schedule["current_period"] == ("promotion" if discounted else "list")
    assert schedule["timezone"] == "Asia/Shanghai"
    assert schedule["ends_at"] == "2026-12-31T16:00:00Z"
    assert schedule["rate_locked_at"] == "authorization"


def test_hy4_price_is_locked_at_authorization_across_promotion_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.catalog import effective_endpoint
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.pricing import _customer_price
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

    endpoint = ModelEndpoint(
        id=f"{MODEL}@tencent:credits", model_id=MODEL, provider="tencent", usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=834_000,
        completion_price_microdollars_per_million_tokens=2_501_000,
    )
    authorized_at = datetime(2026, 12, 31, 15, 59, 59, tzinfo=UTC)
    selected = effective_endpoint(endpoint, at=authorized_at)
    assert selected.prompt_price_microdollars_per_million_tokens == _customer_price(417_000)
    assert selected.completion_price_microdollars_per_million_tokens == _customer_price(1_250_500)
    assert selected.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == _customer_price(21_000)
    monkeypatch.setattr("trusted_router.provider_lifecycle._utc_now", lambda: datetime(2027, 1, 1, tzinfo=UTC))
    assert _endpoint_cost_microdollars(endpoint, 1_000_000, 1_000_000, effective_at=authorized_at) == (
        _customer_price(417_000) + _customer_price(1_250_500)
    )


@pytest.mark.parametrize("at,period", [
    (datetime(2026, 11, 4, tzinfo=UTC), "Promotion"),
    (datetime(2027, 1, 1, tzinfo=UTC), "List"),
])
def test_hy4_future_serving_page_discloses_discount_expiry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, at: datetime, period: str,
) -> None:
    from trusted_router import catalog

    # A synthetic serving route tests the rendering contract, not the held
    # production account's entitlement. Monkeypatch restores the registry.
    preview = catalog.MODELS["tencent/hy4-preview"]
    template = next(e for e in catalog_vehicles.registry_endpoints().values()
                    if e.model_id == preview.id and e.provider == "tencent" and e.usage_type == "Credits")
    endpoint = replace(template, id=f"{MODEL}@tencent/prepaid", model_id=MODEL, upstream_id="hy4")
    monkeypatch.setitem(catalog.MODELS, MODEL, replace(preview, id=MODEL, name="Hy4"))
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
    monkeypatch.setattr("trusted_router.provider_lifecycle._utc_now", lambda: at)
    response = client.get(f"/models/{MODEL}/pricing")
    assert response.status_code == 200
    text = " ".join(BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True).split())
    assert "50% off list prices" in text
    assert "2026-11-03T16:00:00Z" in text
    assert "2026-12-31T16:00:00Z" in text
    assert f"{period} rates currently apply" in text
    assert "locked at authorization" in text
    assert "Rates effective ." not in text
