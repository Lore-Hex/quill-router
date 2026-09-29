from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.providers import _direct_openai, tencent
from trusted_router.provider_lifecycle import (
    TENCENT_OFF_PEAK_PRICES,
    ProviderPrice,
    provider_price_microdollars,
    provider_pricing_schedule,
)

FIXTURES = Path(__file__).parent / "fixtures/pricing"


def prices_html() -> str:
    return (FIXTURES / "tencent_pricing_2026-09-29.html").read_text()


def models_html() -> str:
    return (FIXTURES / "tencent_models_2026-09-29.html").read_text()


def test_tencent_uses_singapore_usd_not_cheaper_guangzhou_prices() -> None:
    prices = tencent.parse_prices(prices_html())
    assert prices["glm-5.3"].prompt_micro_per_m == 1_400_000
    assert prices["glm-5.3-flash"].completion_micro_per_m == 500_000
    assert prices["mimo-v2.6-pro"].tiers[0].prompt_cached_micro_per_m == 3_600
    assert prices["hy3"].prompt_micro_per_m == 132_000
    tiers = prices["minimax-m3"].tiers
    assert [t.max_prompt_tokens for t in tiers] == [524_288, None]
    assert [t.completion_micro_per_m for t in tiers] == [1_200_000, 2_400_000]
    assert prices["deepseek-v4.1-flash"].prompt_micro_per_m == 150_000
    assert all("vendor direct" not in key for key in prices)


@pytest.mark.parametrize("old,new", [
    ("Singapore", "Somewhere"), ("USD / million tokens", "CNY / million tokens"),
    ("Input length 512k+", "Input length 256k+"),
])
def test_tencent_rejects_ambiguous_pricing(old: str, new: str) -> None:
    with pytest.raises(ValueError):
        tencent.parse_prices(prices_html().replace(old, new))


def test_tencent_rowspans_support_standard_html_and_vendor_placeholders() -> None:
    from bs4 import BeautifulSoup
    for placeholder in ("", '<td rowspan="0"></td>'):
        html = f'<table><tr><td rowspan="2">model</td><td>1</td></tr><tr>{placeholder}<td>2</td></tr></table>'
        table = BeautifulSoup(html, "html.parser").find("table")
        assert table is not None
        assert tencent._rows(table) == [["model", "1"], ["model", "2"]]


def test_tencent_capabilities_do_not_mix_media_or_retiring_models() -> None:
    models = tencent.parse_models(models_html())
    assert models["mimo-v2.6-pro"]["context_length"] == 1_048_576
    assert models["hy3"]["context_length"] == 262_144
    assert models["glm-5.3-flash"]["input_modalities"] == ["text", "image"]
    assert "function-calling" in models["glm-5.3-flash"]["supported_features"]
    assert "hy-image-v3" not in models
    assert "glm-5" not in models
    assert "deepseek/deepseek-flash" not in models


def test_tencent_manifest_routes_only_priced_canary_successes() -> None:
    import json

    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS

    rows = json.loads(tencent.MANIFEST_PATH.read_text())["models"]
    eligible = {row["id"]: row for row in rows if row.get("routable") is True}
    endpoints = [e for e in MODEL_ENDPOINTS.values() if e.provider == "tencent"]
    assert eligible
    assert {e.model_id for e in endpoints} == set(eligible)
    for endpoint in endpoints:
        row = eligible[endpoint.model_id]
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
    provider = PROVIDERS["tencent"]
    assert provider.supports_prepaid and provider.supports_byok
    assert provider.provider_e2ee is not True
    assert provider.provider_confidential_compute is not True
    assert provider.provider_zero_data_retention is not True


def test_tencent_discovery_joins_exact_catalog_prices_and_canaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("TENCENT_API_KEY", "test-key")
    monkeypatch.setattr(tencent.CATALOG, "manifest_path", tmp_path / "tencent.json")
    monkeypatch.setattr(tencent, "fetch_html", lambda url: models_html() if url == tencent.MODELS_DOC_URL else prices_html())
    rows = [
        {"id": "hy3", "status": "online"},
        {"id": "glm-5.3-flash", "status": "online"},
        {"id": "mimo-v2.6-pro", "status": "online"},
        {"id": "deepseek-v4-pro-0813", "status": "online"},
        {"id": "deepseek-v4-flash", "status": "discontinued"},
        {"id": "glm-5.1", "status": "pre-offline"},
        {"id": "hy-image-v3", "status": "online"},
        {"id": "hy999-unpriced", "status": "online"},
    ]
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": rows})
    probes: list[dict[str, Any]] = []

    def probe(**kwargs: Any) -> bool:
        probes.append(kwargs)
        return kwargs["model"] != "glm-5.3-flash"

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    result = tencent.fetch()
    assert set(result.prices) == {"tencent/hy3", "z-ai/glm-5.3-flash", "xiaomi/mimo-v2.6-pro", "deepseek/deepseek-v4-pro-0813"}
    assert {p["model"] for p in probes} == {"hy3", "glm-5.3-flash", "mimo-v2.6-pro", "deepseek-v4-pro-0813"}
    assert all(p["base_url"] == "https://tokenhub-intl.tencentcloudmaas.com/v1" for p in probes)
    assert tencent.CATALOG.discovered_rows["z-ai/glm-5.3-flash"]["routable"] is False
    frozen = tencent.CATALOG.discovered_rows["deepseek/deepseek-v4-pro-0813"]
    assert frozen["routable"] is False
    assert frozen["routable_reason"] == "immutable-release-route-set"
    assert tencent.CATALOG.discovered_rows["xiaomi/mimo-v2.6-pro"]["max_output_tokens"] == 131_072
    assert "function-calling" in tencent.CATALOG.discovered_rows["tencent/hy3"]["supported_features"]
    tencent.write_provider_manifest(result)
    import json
    manifest = json.loads((tmp_path / "tencent.json").read_text())
    assert manifest["provider"] == "tencent"
    assert "test-key" not in json.dumps(manifest)
    probes.clear()
    tencent.fetch()
    assert [p["model"] for p in probes] == ["glm-5.3-flash"]


@pytest.mark.parametrize("at,peak", [
    ("2026-09-29T00:59:59Z", False), ("2026-09-29T01:00:00Z", True),
    ("2026-09-29T03:59:59Z", True), ("2026-09-29T04:00:00Z", False),
    ("2026-09-29T06:00:00Z", True), ("2026-09-29T09:59:59Z", True),
    ("2026-09-29T10:00:00Z", False), ("2026-10-03T02:00:00Z", False),
    ("2026-10-04T02:00:00Z", False), ("2026-10-05T02:00:00Z", True),
])
def test_tencent_prices_every_billable_direction_on_its_own_schedule(at: str, peak: bool) -> None:
    for model_id, price in TENCENT_OFF_PEAK_PRICES.items():
        factor = 2 if peak else 1
        assert provider_price_microdollars("tencent", model_id, at=at) == ProviderPrice(
            price.prompt_microdollars_per_million_tokens * factor,
            price.completion_microdollars_per_million_tokens * factor,
            (price.prompt_cached_microdollars_per_million_tokens or 0) * factor,
        )
        schedule = provider_pricing_schedule("tencent", model_id, at=at)
        assert schedule is not None
        assert schedule["current_period"] == ("peak" if peak else "off_peak")
        assert schedule["rate_locked_at"] == "authorization"
        assert schedule["timezone"] == "Asia/Shanghai"
    # Tencent Pro remains its own model and price, not DeepSeek direct's Flash redirect.
    assert provider_price_microdollars("tencent", "deepseek/deepseek-v4-pro-0813", at=at) != provider_price_microdollars("deepseek", "deepseek/deepseek-v4-pro-0813", at=at)
    assert provider_price_microdollars("tencent", "z-ai/glm-5.3", at=at) is None


def test_tencent_billing_uses_authorization_time_not_settlement_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.catalog import effective_endpoint
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.pricing import _customer_price
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
    endpoint = ModelEndpoint(
        id="deepseek/deepseek-v4.1-flash@tencent:credits",
        model_id="deepseek/deepseek-v4.1-flash", provider="tencent", usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=150_000,
        completion_price_microdollars_per_million_tokens=600_000,
    )
    # Verify the existing cost selector contract rather than introducing a
    # Tencent-specific reserve/settle path.
    peak = effective_endpoint(endpoint, at=datetime(2026, 9, 29, 2, tzinfo=UTC))
    assert peak.prompt_price_microdollars_per_million_tokens == _customer_price(300_000)
    monkeypatch.setattr("trusted_router.provider_lifecycle._utc_now", lambda: datetime(2026, 9, 29, 12, tzinfo=UTC))
    cost = _endpoint_cost_microdollars(endpoint, 1_000_000, 1_000_000, effective_at=datetime(2026, 9, 29, 2, tzinfo=UTC))
    assert cost == _customer_price(300_000) + _customer_price(1_200_000)
