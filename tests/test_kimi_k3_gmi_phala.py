from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.providers import gmi
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import PRIVACY_TIER_STANDARD
from trusted_router.catalog_privacy import (
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
)

KIMI_K3 = "moonshotai/kimi-k3"


def test_gmi_hourly_parser_discovers_kimi_k3_exact_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {
                "id": KIMI_K3,
                "pricing": {
                    "prompt": "0.000003",
                    "completion": "0.000015",
                    "input_cache_read": "0.0000003",
                },
            }
        ]
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(gmi.httpx, "Client", FakeClient)

    result = gmi.fetch()

    assert result.prices[KIMI_K3].prompt_micro_per_m == 3_000_000
    assert result.prices[KIMI_K3].completion_micro_per_m == 15_000_000
    assert result.prices[KIMI_K3].tiers[0].prompt_cached_micro_per_m == 300_000
    assert gmi.UPSTREAM_ID_MAP[KIMI_K3] == KIMI_K3


def test_gmi_kimi_k3_is_a_verified_prepaid_route() -> None:
    endpoint = MODEL_ENDPOINTS[f"{KIMI_K3}@gmi/prepaid"]

    assert endpoint.upstream_id == "moonshotai/kimi-k3"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 3_150_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 15_750_000


def test_phala_kimi_k3_pass_through_is_standard_not_confidential() -> None:
    endpoint = MODEL_ENDPOINTS[f"{KIMI_K3}@phala/prepaid"]

    assert endpoint.upstream_id == "moonshotai/kimi-k3"
    assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD
    assert endpoint_stores_content(endpoint) is True
    assert endpoint_zero_data_retention(endpoint) is False
    assert endpoint_confidential_compute(endpoint) is False
    assert endpoint_e2ee(endpoint) is False


def test_kimi_k3_public_catalog_reports_route_specific_phala_posture(
    client: TestClient,
) -> None:
    response = client.get("/v1/models/moonshotai/kimi-k3/endpoints")

    assert response.status_code == 200
    phala = next(
        row
        for row in response.json()["data"]
        if row["provider"] == "phala" and row["usage_type"] == "Credits"
    )
    assert phala["upstream_id"] == "moonshotai/kimi-k3"
    assert phala["trustedrouter"]["privacy_tier"] == PRIVACY_TIER_STANDARD
    assert phala["trustedrouter"]["stores_content"] is True
    assert phala["trustedrouter"]["provider_zero_data_retention"] is False
    assert phala["trustedrouter"]["provider_confidential_compute"] is False
    assert phala["trustedrouter"]["provider_e2ee"] is False
    assert "pass-through" in phala["trustedrouter"]["provider_policy"].casefold()
