from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.providers import engy
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
from trusted_router.services.inference_errors import default_provider_secret_ref


def _model_row(
    native_id: str,
    *,
    prompt: str,
    completion: str,
    cached: str,
    context_length: int,
) -> dict[str, Any]:
    return {
        "id": native_id,
        "object": "model",
        "owned_by": "engy",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "context_length": context_length,
        "max_model_len": context_length,
        "pricing": {
            "prompt": prompt,
            "completion": completion,
            "request": "0",
            "image": "0",
            "input_cache_read": cached,
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            _model_row(
                "glm-5.3-flash",
                prompt="0.000000135",
                completion="0.00000045",
                cached="0.000000027",
                context_length=262_144,
            ),
            _model_row(
                "glm-5.2",
                prompt="0.00000068",
                completion="0.0000015",
                cached="0.00000018",
                context_length=262_144,
            ),
            _model_row(
                "qwen3.6-35b-a3b",
                prompt="0.000000045",
                completion="0.0000003",
                cached="0.000000015",
                context_length=208_192,
            ),
        ],
    }


def test_engy_parser_preserves_exact_prices_models_and_cache_rates() -> None:
    prices, discovered = engy._parse_catalog(_payload())

    assert set(prices) == {
        "z-ai/glm-5.2",
        "z-ai/glm-5.3-flash",
        "qwen/qwen3.6-35b-a3b",
    }
    assert prices["z-ai/glm-5.3-flash"].prompt_micro_per_m == 135_000
    assert prices["z-ai/glm-5.3-flash"].completion_micro_per_m == 450_000
    assert prices["z-ai/glm-5.3-flash"].tiers[0].prompt_cached_micro_per_m == 27_000
    assert prices["z-ai/glm-5.2"].prompt_micro_per_m == 680_000
    assert prices["z-ai/glm-5.2"].completion_micro_per_m == 1_500_000
    assert prices["z-ai/glm-5.2"].tiers[0].prompt_cached_micro_per_m == 180_000
    assert prices["qwen/qwen3.6-35b-a3b"].prompt_micro_per_m == 45_000
    assert prices["qwen/qwen3.6-35b-a3b"].completion_micro_per_m == 300_000
    assert prices["qwen/qwen3.6-35b-a3b"].tiers[0].prompt_cached_micro_per_m == 15_000
    assert discovered["z-ai/glm-5.2"]["upstream_id"] == "glm-5.2"
    assert discovered["z-ai/glm-5.3-flash"]["upstream_id"] == "glm-5.3-flash"
    assert discovered["qwen/qwen3.6-35b-a3b"]["context_length"] == 208_192
    assert "prompt_caching" in discovered["z-ai/glm-5.2"]["supported_features"]
    assert "tools" in discovered["z-ai/glm-5.2"]["supported_features"]
    assert "structured_outputs" in discovered["z-ai/glm-5.2"]["supported_features"]
    assert "tools" not in discovered["qwen/qwen3.6-35b-a3b"]["supported_features"]


def test_engy_parser_ignores_rows_not_owned_by_engy() -> None:
    payload = _payload()
    payload["data"].append(
        _model_row(
            "glm-5.3",
            prompt="0.1",
            completion="0.2",
            cached="0.01",
            context_length=1_000_000,
        )
    )
    payload["data"][-1]["owned_by"] = "untrusted"

    prices, discovered = engy._parse_catalog(payload)

    assert "z-ai/glm-5.3" not in prices
    assert "z-ai/glm-5.3" not in discovered


def test_engy_parser_auto_normalizes_future_unqualified_model_families() -> None:
    payload = {
        "data": [
            _model_row(
                "glm-5.4-flash",
                prompt="0.0000002",
                completion="0.0000006",
                cached="0.00000004",
                context_length=1_048_576,
            )
        ]
    }

    prices, discovered = engy._parse_catalog(payload)

    assert prices["z-ai/glm-5.4-flash"].prompt_micro_per_m == 200_000
    assert discovered["z-ai/glm-5.4-flash"]["upstream_id"] == "glm-5.4-flash"


def test_engy_fetch_requires_key_and_runs_live_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return copy.deepcopy(_payload())

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setenv("ENGY_API_KEY", "test-key")
    monkeypatch.setattr(engy.httpx, "Client", FakeClient)
    canaries: list[dict[str, object]] = []
    monkeypatch.setattr(
        engy,
        "probe_openai_chat",
        lambda **kwargs: canaries.append(kwargs) is None,
    )

    result = engy.fetch()

    assert set(result.prices) == set(engy.EXPECTED_MODELS)
    assert canaries == [
        {
            "base_url": "https://api.engy.ai/v1",
            "api_key": "test-key",
            "model": "glm-5.2",
            "max_tokens": 16,
        }
    ]
    assert engy._LIVE_CANARY_OK is True


def test_engy_fetch_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENGY_API_KEY is required"):
        engy.fetch()


def test_engy_is_prepaid_zdr_but_not_confidential_or_byok() -> None:
    provider = PROVIDERS["engy"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is False
    assert provider.provider_zero_data_retention is True
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert OPENAI_COMPATIBLE_PROVIDERS["engy"] == (
        ("ENGY_API_KEY",),
        "https://api.engy.ai/v1",
    )
    assert default_provider_secret_ref("engy") == "env://ENGY_API_KEY"


def test_engy_manifest_routes_use_exact_upstream_ids_and_customer_prices() -> None:
    endpoints = [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "engy"]
    expected_upstream_ids = {
        "glm-5.2",
        "glm-5.3-flash",
        "qwen3.6-35b-a3b",
    }
    assert expected_upstream_ids <= {endpoint.upstream_id for endpoint in endpoints}

    manifest = json.loads(engy.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_upstream_ids = {
        row["upstream_id"]
        for row in manifest["models"]
        if row.get("model_type") == "chat" and row.get("routable") is not False
    }
    assert {endpoint.upstream_id for endpoint in endpoints} == manifest_upstream_ids
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    glm = next(endpoint for endpoint in endpoints if endpoint.model_id == "z-ai/glm-5.2")
    assert glm.prompt_price_microdollars_per_million_tokens == 717_400
    assert glm.completion_price_microdollars_per_million_tokens == 1_582_500
    assert glm.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 189_900


def test_engy_hourly_refresh_and_secret_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names)
        for slug, url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    assert discoverable["engy"] == (
        "https://api.engy.ai/v1/models",
        ("ENGY_API_KEY",),
    )

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    rollout = (root / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert 'ensure_secret_from_env_file "ENGY_API_KEY" "trustedrouter-engy-api-key"' in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-engy-api-key"' in secrets
    assert 'add_secret_env_if_exists "ENGY_API_KEY" "trustedrouter-engy-api-key"' in rollout
    assert "ENGY_API_KEY:trustedrouter-engy-api-key" in workflow
