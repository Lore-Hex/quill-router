from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts.pricing import base
from scripts.pricing.base import ModelPrice
from scripts.pricing.parsers import deepseek as parser
from scripts.pricing.providers import deepseek
from trusted_router import catalog
from trusted_router import provider_lifecycle as lifecycle
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_lifecycle import ProviderPrice
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

CUTOVER = datetime(2026, 9, 10, 4, tzinfo=UTC)
FLASH = "deepseek/deepseek-flash"
PRO = "deepseek/deepseek-v4-pro"
DATED_PRO = "deepseek/deepseek-v4-pro-0813"
DATED_FLASH = "deepseek/deepseek-v4-flash-0731"


@pytest.mark.parametrize("model", [FLASH, "deepseek/deepseek-v4-flash", PRO])
@pytest.mark.parametrize(("at", "peak"), [
    (CUTOVER, False),
    (datetime(2026, 9, 10, 5, 59, 59, tzinfo=UTC), False),
    (datetime(2026, 9, 10, 6, tzinfo=UTC), True),
    (datetime(2026, 9, 10, 9, 59, 59, tzinfo=UTC), True),
    (datetime(2026, 9, 10, 10, tzinfo=UTC), False),
    (datetime(2026, 9, 11, 0, 59, 59, tzinfo=UTC), False),
    (datetime(2026, 9, 11, 1, tzinfo=UTC), True),
    (datetime(2026, 9, 11, 3, 59, 59, tzinfo=UTC), True),
    (datetime(2026, 9, 11, 4, tzinfo=UTC), False),
    (datetime(2026, 9, 12, 2, tzinfo=UTC), False),
    (datetime(2026, 9, 13, 7, tzinfo=UTC), False),
    (datetime(2026, 9, 14, 2, tzinfo=UTC), True),
])
def test_announced_flash_prices_apply_to_direct_rolling_routes(
    model: str, at: datetime, peak: bool,
) -> None:
    expected = ProviderPrice(300_000, 1_200_000, 6_000) if peak else ProviderPrice(
        150_000, 600_000, 3_000,
    )
    assert lifecycle.provider_price_microdollars("deepseek", model, at=at) == expected
    assert lifecycle.provider_price_microdollars("baseten", model, at=at) is None


def test_no_early_price_cut_and_historical_pro_billing_is_preserved() -> None:
    before = CUTOVER - timedelta(microseconds=1)
    for model in (PRO, DATED_PRO):
        assert lifecycle.provider_price_microdollars("deepseek", model, at=before) == (
            ProviderPrice(1_320_000, 3_960_000, 44_000)
        )
    assert lifecycle.provider_price_microdollars("deepseek", FLASH, at=before) == (
        ProviderPrice(440_000, 1_320_000, 14_000)
    )


@pytest.mark.parametrize(("model", "upstream"), [
    (DATED_PRO, "deepseek-v4-pro"), (DATED_FLASH, "deepseek-v4-flash"),
])
def test_dated_routes_cannot_silently_follow_upstream_flash_redirect(
    model: str, upstream: str,
) -> None:
    assert not lifecycle.provider_model_retired("deepseek", model, upstream, at=CUTOVER - timedelta(seconds=1))
    assert lifecycle.provider_model_retired("deepseek", model, upstream, at=CUTOVER)
    assert not lifecycle.provider_model_retired("baseten", model, upstream, at=CUTOVER)
    assert not lifecycle.provider_model_retired("deepseek", PRO, "deepseek-v4-pro", at=CUTOVER)


def test_stale_process_filters_dated_pro_without_changing_other_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: CUTOVER)
    routes = catalog.endpoints_for_model(DATED_PRO)
    assert {route.provider for route in routes} == {"baseten", "fireworks"}


def test_schedule_discloses_pro_redirect_and_new_effective_time() -> None:
    schedule = lifecycle.provider_pricing_schedule("deepseek", PRO, at=CUTOVER)
    assert schedule["effective_at"] == "2026-09-10T04:00:00Z"
    assert schedule["upstream_redirect"] == {
        "model": FLASH, "reason": "DeepSeek replaces Pro with V4.1 Flash until V4.1 Pro launches",
    }
    assert schedule["weekend_off_peak"]["timezone"] == "UTC"
    assert schedule["rate_locked_at"] == "authorization"


def test_discovery_recognizes_live_deepseek_flash_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(deepseek, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: CUTOVER)
    monkeypatch.setattr(base, "fetch_html", lambda *a, **k: "<p>deepseek-v4-flash deepseek-v4-pro</p>")
    monkeypatch.setattr(deepseek, "fetch_json", lambda *a, **k: {
        "data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}],
    })
    result = deepseek.fetch()
    assert result.prices[FLASH] == ModelPrice(150_000, 600_000, prompt_cached_micro_per_m=3_000)
    assert result.prices[PRO] == result.prices[FLASH]
    assert deepseek._DISCOVERED_MANIFEST_ROWS[FLASH]["upstream_id"] == "deepseek-flash"
    assert deepseek._DISCOVERED_MANIFEST_ROWS[FLASH]["context_length"] == 1_048_576
    assert "function-calling" in deepseek._DISCOVERED_MANIFEST_ROWS[FLASH]["supported_features"]
    assert DATED_FLASH not in result.prices


def test_refresh_accepts_pricing_page_after_native_flash_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(deepseek, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: CUTOVER)
    monkeypatch.setattr(base, "fetch_html", lambda *a, **k: "<p>deepseek-flash deepseek-v4-pro</p>")
    monkeypatch.setattr(base, "self_heal_parser", lambda **k: pytest.fail("no self-heal expected"))
    result = deepseek.fetch()
    assert result.prices[FLASH] == result.prices["deepseek/deepseek-v4-flash"]


@pytest.mark.parametrize("after", [False, True])
def test_runtime_required_models_do_not_break_discovery_after_cutover(
    monkeypatch: pytest.MonkeyPatch, after: bool,
) -> None:
    at = CUTOVER if after else CUTOVER - timedelta(seconds=1)
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: at)
    monkeypatch.setattr(deepseek, "UPSTREAM_ID_MAP", {})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(base, "fetch_html", lambda *a, **k: "<p>deepseek-v4-flash deepseek-v4-pro</p>")
    monkeypatch.setattr(base, "self_heal_parser", lambda **k: pytest.fail("no self-heal expected"))
    base.configure_runtime_required_models({"deepseek": {FLASH, PRO, DATED_PRO, DATED_FLASH}})
    try:
        result = deepseek.fetch()
    finally:
        base.configure_runtime_required_models({})
    assert FLASH in result.prices
    assert (DATED_PRO in result.prices) is not after
    assert (DATED_FLASH in result.prices) is not after


def test_parser_handles_official_rowspans_without_stale_fallback() -> None:
    html = """<table>
    <tr><th>MODEL</th><th>deepseek-flash</th><th>deepseek-v4-pro</th></tr>
    <tr><td rowspan="6">PRICING</td><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.003</td></tr>
    <tr><td>PEAK</td><td>$0.006</td><td>$0.006</td></tr>
    <tr><td rowspan="2">1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.15</td></tr>
    <tr><td>PEAK</td><td>$0.3</td><td>$0.3</td></tr>
    <tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$0.6</td></tr>
    <tr><td>PEAK</td><td>$1.2</td><td>$1.2</td></tr>
    </table>"""
    prices = parser.parse(html)
    assert prices[FLASH] == prices[PRO] == {
        "prompt_micro_per_m": 150_000, "completion_micro_per_m": 600_000,
        "prompt_cached_micro_per_m": 3_000,
    }


def test_settlement_uses_authorization_quote_across_launch() -> None:
    endpoint = catalog.MODEL_ENDPOINTS[f"{PRO}@deepseek/prepaid"]
    # Published customer prices retain the existing markup and minimum rate.
    for at, prompt, cached, output in [
        (CUTOVER - timedelta(seconds=1), 1_392_600, 46_420, 4_177_800),
        (CUTOVER, 158_250, 10_000, 633_000),
    ]:
        assert _endpoint_cost_microdollars(
            endpoint, 100_000, 200_000, cache_read_tokens=900_000, effective_at=at,
        ) == (prompt // 10 + cached * 9 // 10 + output // 5)


def test_public_new_flash_route_and_pro_redirect_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: CUTOVER)
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    response = client.get(f"/v1/models/{FLASH}/endpoints")
    assert response.status_code == 200
    row = next(row for row in response.json()["data"] if row["provider"] == "deepseek")
    assert row["pricing"] == {
        "prompt": "0.00000015825", "completion": "0.000000633",
        "input_cache_read": "0.00000001",
    }
    page = client.get(f"/models/{PRO}/pricing")
    assert page.status_code == 200
    assert "2026-09-10T04:00:00Z" in page.text
    assert "DeepSeek replaces Pro with V4.1 Flash" in page.text
    assert "Weekends are off-peak all day in UTC" in page.text


def test_flash_route_can_authorize_locally_and_advertises_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: CUTOVER)
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    key_response = client.post(
        "/v1/keys", headers={"x-trustedrouter-user": "flash-launch-test@example.com"},
        json={"name": "Flash launch local test"},
    )
    assert key_response.status_code == 201
    authorized = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key_response.json()["data"]["hash"], "model": FLASH,
        "estimated_input_tokens": 32, "max_output_tokens": 32,
        "provider": {"only": ["deepseek"]},
    })
    assert authorized.status_code == 200, authorized.text
    assert "tools" in catalog.MODELS[FLASH].supported_parameters
