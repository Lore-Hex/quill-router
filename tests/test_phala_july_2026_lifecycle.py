from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import phala
from tests.lifecycle_clock import catalog_predates
from trusted_router import provider_lifecycle
from trusted_router.catalog import (
    MODELS,
    endpoint_for_id,
    endpoints_for_model,
    model_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.main import create_app

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
        assert provider_lifecycle.provider_model_retired("phala", model_id, upstream_id, at=_CUTOFF)


def test_phala_retirement_is_provider_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    glm_providers = {endpoint.provider for endpoint in endpoints_for_model("z-ai/glm-4.7")}
    qwen_providers = {
        endpoint.provider for endpoint in endpoints_for_model("qwen/qwen3-30b-a3b-instruct-2507")
    }

    assert "phala" not in glm_providers
    assert glm_providers
    assert "phala" not in qwen_providers
    # Retirement is provider-scoped: Alibaba still serves this exact Qwen
    # revision after Phala's route is removed -- until Alibaba retires it too,
    # at which point no provider serves it and the model leaves the catalog.
    if catalog_predates(provider_lifecycle.ALIBABA_OCTOBER_2026_RETIREMENT_AT):
        assert qwen_providers == {"alibaba"}


def test_non_confidential_phala_qwen_route_is_not_published() -> None:
    assert endpoint_for_id("qwen/qwen-2.5-7b-instruct@phala/prepaid") is None
    assert all(
        endpoint.provider != "phala"
        for endpoint in endpoints_for_model("qwen/qwen-2.5-7b-instruct")
    )


def test_public_catalog_uses_effective_price_and_active_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    qwen_price = model_to_openrouter_shape(MODELS["qwen/qwen-2.5-7b-instruct"])
    qwen_endpoints = endpoints_for_model("qwen/qwen-2.5-7b-instruct")
    assert qwen_endpoints
    assert all(endpoint.provider != "phala" for endpoint in qwen_endpoints)
    assert qwen_price["trustedrouter"]["prompt_price_microdollars_per_million_tokens"] == min(
        endpoint.prompt_price_microdollars_per_million_tokens for endpoint in qwen_endpoints
    )

    # Alibaba retires this same Qwen revision on 2026-10-09; after that no
    # provider serves it and the model is absent from MODELS altogether, which
    # is a stronger form of the same assertion.
    if catalog_predates(provider_lifecycle.ALIBABA_OCTOBER_2026_RETIREMENT_AT):
        retired_qwen = model_to_openrouter_shape(MODELS["qwen/qwen3-30b-a3b-instruct-2507"])
        assert all(
            endpoint["provider"] != "phala"
            for endpoint in retired_qwen["trustedrouter"]["endpoints"]
        )
    else:
        assert "qwen/qwen3-30b-a3b-instruct-2507" not in MODELS


def test_phala_hourly_parser_applies_announced_policy() -> None:
    prices = {
        "z-ai/glm-4.7": ModelPrice(1, 2),
        "qwen/qwen3-30b-a3b-instruct-2507": ModelPrice(3, 4),
        "qwen/qwen-2.5-7b-instruct": ModelPrice(40_000, 100_000),
        "z-ai/glm-5.2": ModelPrice(300_000, 2_000_000),
    }

    before = phala._apply_lifecycle_policy(prices, at=_CUTOFF - timedelta(microseconds=1))
    after = phala._apply_lifecycle_policy(prices, at=_CUTOFF)

    assert set(before) == set(prices)
    assert before["qwen/qwen-2.5-7b-instruct"] == ModelPrice(40_000, 100_000)
    assert "z-ai/glm-4.7" not in after
    assert "qwen/qwen3-30b-a3b-instruct-2507" not in after
    assert after["qwen/qwen-2.5-7b-instruct"] == ModelPrice(100_000, 200_000)
    assert after["z-ai/glm-5.2"] == prices["z-ai/glm-5.2"]


def test_phala_parser_publishes_confidential_routes_and_verified_k3_pass_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = {
        "data": [
            {
                "id": "phala/glm-5.2",
                "pricing": {"prompt": "0.0000003", "completion": "0.000002"},
            },
            {
                "id": "openai/gpt-5.5",
                "pricing": {"prompt": "0.000005", "completion": "0.00003"},
            },
            {
                "id": "moonshotai/kimi-k3",
                "name": "Kimi K3",
                "context_length": 1_048_576,
                "max_output_length": 65_535,
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "supported_features": ["reasoning", "tools"],
                "pricing": {
                    "prompt": "0.000003",
                    "completion": "0.000015",
                    "input_cache_read": "0.0000015",
                },
            },
            {
                "id": "unmapped/ordinary-pass-through",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
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

    monkeypatch.setattr(phala.httpx, "Client", FakeClient)

    result = phala.fetch()

    assert set(result.prices) == {"moonshotai/kimi-k3", "z-ai/glm-5.2"}
    assert phala.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "phala/glm-5.2"
    assert phala.UPSTREAM_ID_MAP["moonshotai/kimi-k3"] == "moonshotai/kimi-k3"
    assert result.prices["moonshotai/kimi-k3"] == ModelPrice(
        3_000_000,
        15_000_000,
        prompt_cached_micro_per_m=1_500_000,
    )
    assert (
        phala._DISCOVERED_MANIFEST_ROWS["moonshotai/kimi-k3"]["upstream_id"] == "moonshotai/kimi-k3"
    )
    assert "openai/gpt-5.5" not in phala._DISCOVERED_MANIFEST_ROWS

    manifest_path = tmp_path / "phala.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "phala",
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "display_name": "GLM 5.2",
                        "title": "phala/glm-5.2",
                        "model_type": "chat",
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "endpoints": ["chat/completions"],
                        "status": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(phala, "MANIFEST_PATH", manifest_path)

    phala.write_provider_manifest(result)

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows_by_id = {row["id"]: row for row in written["models"]}
    assert rows_by_id["moonshotai/kimi-k3"]["upstream_id"] == "moonshotai/kimi-k3"
    assert rows_by_id["moonshotai/kimi-k3"]["input_token_price_per_m"] == 3_000_000
    assert "openai/gpt-5.5" not in rows_by_id
    assert "unmapped/ordinary-pass-through" not in rows_by_id


def test_non_confidential_phala_route_is_rejected_before_authorization(
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
    assert authorize.status_code == 400, authorize.text
    assert authorize.json()["error"]["type"] == "model_not_supported"
