from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import base
from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import deepseek
from trusted_router import catalog
from trusted_router import provider_lifecycle as lifecycle
from trusted_router.provider_lifecycle import ProviderPrice
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

PRO = "deepseek/deepseek-v4-pro"
PINNED_PRO = "deepseek/deepseek-v4-pro-0813"
FLASH = "deepseek/deepseek-flash"
REDIRECT_AT = datetime(2026, 9, 14, 4, tzinfo=UTC)


@pytest.mark.parametrize("model", [PRO, PINNED_PRO])
@pytest.mark.parametrize(("at", "peak"), [
    (datetime(2026, 9, 10, 4, tzinfo=UTC), False),
    (datetime(2026, 9, 10, 7, tzinfo=UTC), True),
    (datetime(2026, 9, 10, 15, tzinfo=UTC), False),
    (datetime(2026, 9, 11, 17, tzinfo=UTC), False),
    (datetime(2026, 9, 12, 7, tzinfo=UTC), False),
    (REDIRECT_AT - timedelta(microseconds=1), True),
])
def test_pro_preserves_own_prices_until_actual_redirect(model, at, peak):
    expected = ProviderPrice(1_320_000, 3_960_000, 44_000) if peak else ProviderPrice(
        660_000, 1_980_000, 22_000,
    )
    assert lifecycle.provider_price_microdollars("deepseek", model, at=at) == expected


def test_only_flash_migrates_on_september10():
    at = datetime(2026, 9, 10, 15, tzinfo=UTC)
    assert lifecycle.provider_price_microdollars("deepseek", FLASH, at=at) == ProviderPrice(
        150_000, 600_000, 3_000,
    )
    assert not lifecycle.provider_model_retired("deepseek", PINNED_PRO, "deepseek-v4-pro", at=at)
    assert lifecycle.provider_model_retired(
        "deepseek", "deepseek/deepseek-v4-flash-0731", "deepseek-v4-flash", at=at,
    )


def test_pro_pinned_identity_retires_only_at_its_own_redirect():
    assert not lifecycle.provider_model_retired(
        "deepseek", PINNED_PRO, "deepseek-v4-pro", at=REDIRECT_AT - timedelta(seconds=1),
    )
    assert lifecycle.provider_model_retired("deepseek", PINNED_PRO, "deepseek-v4-pro", at=REDIRECT_AT)
    assert not lifecycle.provider_model_retired("baseten", PINNED_PRO, at=REDIRECT_AT)


def test_public_schedule_does_not_claim_early_pro_redirect():
    before = lifecycle.provider_pricing_schedule("deepseek", PRO, at=REDIRECT_AT - timedelta(seconds=1))
    assert "upstream_redirect" not in before
    assert before["weekend_off_peak"]["timezone"] == "UTC"
    after = lifecycle.provider_pricing_schedule("deepseek", PRO, at=REDIRECT_AT)
    assert after["effective_at"] == "2026-09-14T04:00:00Z"
    assert after["upstream_redirect"]["model"] == FLASH


def test_discovery_publishes_distinct_live_pro_price_and_flash_version(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: datetime(2026, 9, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(base, "fetch_html", lambda *a, **k: "<p>deepseek-flash deepseek-v4-pro</p>")
    monkeypatch.setattr(deepseek, "fetch_json", lambda *a, **k: {
        "data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}],
    })
    result = deepseek.fetch()
    assert result.prices[PRO] == ModelPrice(660_000, 1_980_000, prompt_cached_micro_per_m=22_000)
    assert result.prices[FLASH] == ModelPrice(150_000, 600_000, prompt_cached_micro_per_m=3_000)
    assert deepseek._DISCOVERED_MANIFEST_ROWS[FLASH]["display_name"] == "DeepSeek V4.1 Flash (rolling)"


def test_pro_customer_quote_before_redirect_is_not_flash_priced():
    endpoint = catalog.MODEL_ENDPOINTS[f"{PRO}@deepseek/prepaid"]
    assert _endpoint_cost_microdollars(
        endpoint, 100_000, 200_000, cache_read_tokens=900_000,
        effective_at=datetime(2026, 9, 10, 15, tzinfo=UTC),
    ) == (696_300 // 10 + 23_210 * 9 // 10 + 2_088_900 // 5)
