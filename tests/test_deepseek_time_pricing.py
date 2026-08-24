from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.parsers import deepseek as deepseek_parser
from trusted_router.catalog import MODEL_ENDPOINTS, effective_endpoint
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_lifecycle import (
    DEEPSEEK_V4_PRICING_EFFECTIVE_AT,
    DEEPSEEK_WEEKEND_OFF_PEAK_EFFECTIVE_AT,
    ProviderPrice,
    provider_price_microdollars,
    provider_pricing_schedule,
)
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
from trusted_router.storage import STORE

_FLASH = "deepseek/deepseek-v4-flash"
_FLASH_DATED = "deepseek/deepseek-v4-flash-0731"
_PRO = "deepseek/deepseek-v4-pro"


def test_deepseek_parser_uses_off_peak_as_static_schedule_baseline() -> None:
    html = """
    <table>
      <tr><th>MODEL</th><th>deepseek-v4-flash</th><th>deepseek-v4-pro</th></tr>
      <tr><td>OFF-PEAK 1M INPUT TOKENS (CACHE HIT)</td><td>$0.007</td><td>$0.022</td></tr>
      <tr><td>OFF-PEAK 1M INPUT TOKENS (CACHE MISS)</td><td>$0.22</td><td>$0.66</td></tr>
      <tr><td>OFF-PEAK 1M OUTPUT TOKENS</td><td>$0.66</td><td>$1.98</td></tr>
      <tr><td>PEAK 1M INPUT TOKENS (CACHE HIT)</td><td>$0.014</td><td>$0.044</td></tr>
      <tr><td>PEAK 1M INPUT TOKENS (CACHE MISS)</td><td>$0.44</td><td>$1.32</td></tr>
      <tr><td>PEAK 1M OUTPUT TOKENS</td><td>$1.32</td><td>$3.96</td></tr>
    </table>
    """

    assert deepseek_parser.parse(html) == {
        _FLASH: {
            "prompt_micro_per_m": 220_000,
            "completion_micro_per_m": 660_000,
            "prompt_cached_micro_per_m": 7_000,
        },
        _PRO: {
            "prompt_micro_per_m": 660_000,
            "completion_micro_per_m": 1_980_000,
            "prompt_cached_micro_per_m": 22_000,
        },
    }


def test_deepseek_parser_fallback_keeps_announced_off_peak_baseline() -> None:
    assert deepseek_parser.parse(
        "<p>Available models: deepseek-v4-flash and deepseek-v4-pro</p>"
    ) == {
        _FLASH: {
            "prompt_micro_per_m": 220_000,
            "completion_micro_per_m": 660_000,
            "prompt_cached_micro_per_m": 7_000,
        },
        _PRO: {
            "prompt_micro_per_m": 660_000,
            "completion_micro_per_m": 1_980_000,
            "prompt_cached_micro_per_m": 22_000,
        },
    }


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        (_FLASH, ProviderPrice(140_000, 280_000, 2_800)),
        (_FLASH_DATED, ProviderPrice(140_000, 280_000, 2_800)),
        (_PRO, ProviderPrice(435_000, 870_000, 3_625)),
    ],
)
def test_deepseek_direct_prices_stay_flat_before_announced_cutover(
    model_id: str,
    expected: ProviderPrice,
) -> None:
    assert provider_price_microdollars(
        "deepseek",
        model_id,
        at=DEEPSEEK_V4_PRICING_EFFECTIVE_AT - timedelta(seconds=1),
    ) == expected


@pytest.mark.parametrize(
    ("at", "is_peak"),
    [
        (datetime(2026, 8, 16, 16, 0, tzinfo=UTC), False),
        (datetime(2026, 8, 17, 0, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 8, 17, 1, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 17, 3, 59, 59, tzinfo=UTC), True),
        (datetime(2026, 8, 17, 4, 0, tzinfo=UTC), False),
        (datetime(2026, 8, 17, 5, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 8, 17, 6, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 17, 9, 59, 59, tzinfo=UTC), True),
        (datetime(2026, 8, 17, 10, 0, tzinfo=UTC), False),
        (datetime(2026, 8, 17, 23, 59, 59, tzinfo=UTC), False),
    ],
)
def test_deepseek_v4_flash_uses_half_open_utc_peak_windows(
    at: datetime,
    is_peak: bool,
) -> None:
    expected = ProviderPrice(440_000, 1_320_000, 14_000) if is_peak else ProviderPrice(
        220_000,
        660_000,
        7_000,
    )
    assert provider_price_microdollars("deepseek", _FLASH, at=at) == expected


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        # The new rule starts at Sunday midnight in Beijing. A Sunday instant
        # inside an otherwise-peak UTC window must use the off-peak rate.
        (
            datetime(2026, 8, 23, 2, 0, tzinfo=UTC),
            ProviderPrice(220_000, 660_000, 7_000),
        ),
        # Every later Saturday is off-peak all day in Beijing too.
        (
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
            ProviderPrice(220_000, 660_000, 7_000),
        ),
        # The weekday peak schedule remains unchanged.
        (
            datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
            ProviderPrice(440_000, 1_320_000, 14_000),
        ),
    ],
)
def test_deepseek_weekends_are_always_off_peak_in_beijing(
    at: datetime,
    expected: ProviderPrice,
) -> None:
    assert provider_price_microdollars("deepseek", _FLASH, at=at) == expected


def test_deepseek_weekend_rule_does_not_apply_before_announced_cutover() -> None:
    # Saturday morning in Beijing was still governed by the old time-of-day
    # schedule because the notice takes effect Sunday at 00:00 Beijing time.
    assert provider_price_microdollars(
        "deepseek",
        _FLASH,
        at=datetime(2026, 8, 22, 2, 0, tzinfo=UTC),
    ) == ProviderPrice(440_000, 1_320_000, 14_000)

    assert DEEPSEEK_WEEKEND_OFF_PEAK_EFFECTIVE_AT == datetime(
        2026, 8, 22, 16, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("model_id", "off_peak", "peak"),
    [
        (
            _FLASH,
            ProviderPrice(220_000, 660_000, 7_000),
            ProviderPrice(440_000, 1_320_000, 14_000),
        ),
        (
            _PRO,
            ProviderPrice(660_000, 1_980_000, 22_000),
            ProviderPrice(1_320_000, 3_960_000, 44_000),
        ),
    ],
)
def test_deepseek_v4_new_rates_cover_input_output_and_cache(
    model_id: str,
    off_peak: ProviderPrice,
    peak: ProviderPrice,
) -> None:
    assert provider_price_microdollars(
        "deepseek", model_id, at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    ) == off_peak
    assert provider_price_microdollars(
        "deepseek", model_id, at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
    ) == peak


def test_deepseek_schedule_metadata_is_explicit_and_authorization_scoped() -> None:
    assert provider_pricing_schedule(
        "deepseek",
        _PRO,
        at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
    ) == {
        "kind": "time_of_day",
        "timezone": "UTC",
        "effective_at": "2026-08-16T16:00:00Z",
        "current_period": "peak",
        "peak_multiplier": 2,
        "peak_windows": [
            {"start": "01:00", "end": "04:00"},
            {"start": "06:00", "end": "10:00"},
        ],
        "weekend_off_peak": {
            "effective_at": "2026-08-22T16:00:00Z",
            "timezone": "Asia/Shanghai",
            "days": ["Saturday", "Sunday"],
        },
        "rate_locked_at": "authorization",
    }
    assert provider_pricing_schedule(
        "novita",
        _PRO,
        at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
    ) is None


def test_deepseek_schedule_does_not_change_third_party_route_prices() -> None:
    assert provider_price_microdollars(
        "novita",
        _FLASH,
        at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
    ) is None


def test_model_endpoints_publish_direct_deepseek_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trusted_router.provider_lifecycle._utc_now",
        lambda: datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
    )
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    response = client.get("/v1/models/deepseek/deepseek-v4-pro/endpoints")
    assert response.status_code == 200, response.text
    direct = next(row for row in response.json()["data"] if row["provider"] == "deepseek")

    assert direct["pricing"] == {
        "prompt": "0.0000013926",
        "completion": "0.0000041778",
        "input_cache_read": "0.00000004642",
    }
    assert direct["trustedrouter"]["pricing_schedule"]["current_period"] == "peak"
    assert direct["trustedrouter"]["pricing_schedule"]["rate_locked_at"] == "authorization"


def test_deepseek_pricing_page_explains_variable_direct_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trusted_router.provider_lifecycle._utc_now",
        lambda: datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    response = client.get("/models/deepseek/deepseek-v4-pro/pricing")

    assert response.status_code == 200, response.text
    assert "authorization-time pricing" in response.text
    assert "01:00–04:00 UTC" in response.text
    assert "06:00–10:00 UTC" in response.text
    assert "Weekends are off-peak all day in Beijing time" in response.text


@pytest.mark.parametrize(
    ("model_id", "period", "prompt", "completion", "cached"),
    [
        (_FLASH, "off_peak", 232_100, 696_300, 10_000),
        (_FLASH, "peak", 464_200, 1_392_600, 14_770),
        (_PRO, "off_peak", 696_300, 2_088_900, 23_210),
        (_PRO, "peak", 1_392_600, 4_177_800, 46_420),
    ],
)
def test_effective_endpoint_applies_markup_and_real_cached_rate(
    model_id: str,
    period: str,
    prompt: int,
    completion: int,
    cached: int,
) -> None:
    endpoint = MODEL_ENDPOINTS[f"{model_id}@deepseek/prepaid"]
    hour = 2 if period == "peak" else 12
    priced = effective_endpoint(endpoint, at=datetime(2026, 8, 17, hour, 0, tzinfo=UTC))

    assert priced.prompt_price_microdollars_per_million_tokens == prompt
    assert priced.completion_price_microdollars_per_million_tokens == completion
    assert priced.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == cached


def test_deepseek_cached_tokens_are_billed_at_announced_cache_rate() -> None:
    endpoint = MODEL_ENDPOINTS[f"{_PRO}@deepseek/prepaid"]
    actual = _endpoint_cost_microdollars(
        endpoint,
        100_000,
        200_000,
        cache_read_tokens=900_000,
        effective_at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
    )

    assert actual == (
        100_000 * 1_392_600 // 1_000_000
        + 900_000 * 46_420 // 1_000_000
        + 200_000 * 4_177_800 // 1_000_000
    )


def test_gateway_settlement_uses_authorization_time_for_deepseek_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peak_time = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
    off_peak_time = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "trusted_router.provider_lifecycle._utc_now",
        lambda: peak_time,
    )
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "deepseek-pricing@example.com"},
        json={"name": "DeepSeek pricing"},
    )
    assert created.status_code == 201, created.text
    key = created.json()["data"]

    authorized = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": _FLASH,
            "estimated_input_tokens": 100_000,
            "max_output_tokens": 10_000,
            "provider": {"only": ["deepseek"]},
        },
    )
    assert authorized.status_code == 200, authorized.text
    authorization_id = authorized.json()["data"]["authorization_id"]
    authorization = STORE.get_gateway_authorization(authorization_id)
    assert authorization is not None
    authorization.created_at = peak_time.isoformat().replace("+00:00", "Z")

    # Settlement happens during another period. Billing must continue to use
    # the authorization's peak-time quote rather than the wall clock now.
    monkeypatch.setattr(
        "trusted_router.provider_lifecycle._utc_now",
        lambda: off_peak_time,
    )
    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 100_000,
            "actual_output_tokens": 10_000,
            "cache_read_input_tokens": 90_000,
            "request_id": "deepseek-peak-price",
            "elapsed_seconds": 1,
        },
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["cost_microdollars"] == 19_897
