from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import phala
from trusted_router import provider_lifecycle
from trusted_router.catalog import (
    MODELS,
    effective_endpoint,
    endpoint_for_id,
    endpoints_for_model,
    model_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
from trusted_router.storage import STORE

_CUTOFF = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)


def test_phala_retirements_switch_at_announced_instant() -> None:
    before = _CUTOFF - timedelta(microseconds=1)

    for model_id, upstream_id in (
        ("z-ai/glm-4.7", "phala/glm-4.7"),
        (
            "qwen/qwen3-30b-a3b-instruct-2507",
            "phala/qwen3-30b-a3b-instruct-2507",
        ),
    ):
        assert not provider_lifecycle.provider_model_retired(
            "phala", model_id, upstream_id, at=before
        )
        assert provider_lifecycle.provider_model_retired(
            "phala", model_id, upstream_id, at=_CUTOFF
        )


def test_phala_retirement_is_provider_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    glm_providers = {
        endpoint.provider for endpoint in endpoints_for_model("z-ai/glm-4.7")
    }
    qwen_providers = {
        endpoint.provider
        for endpoint in endpoints_for_model("qwen/qwen3-30b-a3b-instruct-2507")
    }

    assert "phala" not in glm_providers
    assert glm_providers
    assert "phala" not in qwen_providers
    # Phala is currently the only live route for this exact Qwen revision.
    assert not qwen_providers


def test_phala_qwen_price_switches_exactly_at_cutoff() -> None:
    endpoint = endpoint_for_id("qwen/qwen-2.5-7b-instruct@phala/prepaid")
    assert endpoint is not None

    before = effective_endpoint(endpoint, at=_CUTOFF - timedelta(microseconds=1))
    after = effective_endpoint(endpoint, at=_CUTOFF)

    assert before.prompt_price_microdollars_per_million_tokens == 44_000
    assert before.completion_price_microdollars_per_million_tokens == 110_000
    assert after.prompt_price_microdollars_per_million_tokens == 110_000
    assert after.completion_price_microdollars_per_million_tokens == 220_000

    assert (
        _endpoint_cost_microdollars(
            endpoint,
            1_000_000,
            1_000_000,
            effective_at=_CUTOFF - timedelta(microseconds=1),
        )
        == 154_000
    )
    assert (
        _endpoint_cost_microdollars(
            endpoint,
            1_000_000,
            1_000_000,
            effective_at=_CUTOFF,
        )
        == 330_000
    )


def test_public_catalog_uses_effective_price_and_active_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    qwen_price = model_to_openrouter_shape(MODELS["qwen/qwen-2.5-7b-instruct"])
    assert qwen_price["trustedrouter"][
        "prompt_price_microdollars_per_million_tokens"
    ] == 110_000
    assert qwen_price["trustedrouter"][
        "completion_price_microdollars_per_million_tokens"
    ] == 220_000

    retired_qwen = model_to_openrouter_shape(
        MODELS["qwen/qwen3-30b-a3b-instruct-2507"]
    )
    assert retired_qwen["trustedrouter"]["prepaid_available"] is False
    assert retired_qwen["trustedrouter"]["byok_available"] is False
    assert retired_qwen["trustedrouter"]["endpoints"] == []


def test_phala_hourly_parser_applies_announced_policy() -> None:
    prices = {
        "z-ai/glm-4.7": ModelPrice(1, 2),
        "qwen/qwen3-30b-a3b-instruct-2507": ModelPrice(3, 4),
        "qwen/qwen-2.5-7b-instruct": ModelPrice(40_000, 100_000),
        "z-ai/glm-5.2": ModelPrice(300_000, 2_000_000),
    }

    before = phala._apply_lifecycle_policy(
        prices, at=_CUTOFF - timedelta(microseconds=1)
    )
    after = phala._apply_lifecycle_policy(prices, at=_CUTOFF)

    assert set(before) == set(prices)
    assert before["qwen/qwen-2.5-7b-instruct"] == ModelPrice(40_000, 100_000)
    assert "z-ai/glm-4.7" not in after
    assert "qwen/qwen3-30b-a3b-instruct-2507" not in after
    assert after["qwen/qwen-2.5-7b-instruct"] == ModelPrice(100_000, 200_000)
    assert after["z-ai/glm-5.2"] == prices["z-ai/glm-5.2"]


def test_settlement_keeps_authorization_time_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(seconds=1),
    )
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "phala-cutover@example.com"},
        json={"name": "phala cutover"},
    )
    assert created.status_code == 201, created.text
    key = created.json()["data"]
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "qwen/qwen-2.5-7b-instruct",
            "provider": {"only": ["phala"]},
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    authorization_id = authorize.json()["data"]["authorization_id"]
    authorization = STORE.get_gateway_authorization(authorization_id)
    assert authorization is not None
    authorization.created_at = (_CUTOFF - timedelta(seconds=1)).isoformat()

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF + timedelta(seconds=1),
    )
    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "request_id": "gw-phala-price-cutover",
            "elapsed_seconds": 1.0,
        },
    )

    assert settle.status_code == 200, settle.text
    assert settle.json()["data"]["cost_microdollars"] == 154
