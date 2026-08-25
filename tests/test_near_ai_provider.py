from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.providers import near_ai
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_ZERO_RETENTION,
)
from trusted_router.catalog_ingest import _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
from trusted_router.catalog_privacy import (
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import e2e_candidate_models


def _catalog_row(
    native_id: str,
    *,
    prompt: str = "0.00000017",
    completion: str = "0.00000035",
    cached: str = "0.000000035",
    owner: str = "nearai",
) -> dict[str, Any]:
    return {
        "id": native_id,
        "owned_by": owner,
        "context_length": 1_048_576,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "pricing": {
            "prompt": prompt,
            "completion": completion,
            "input_cache_read": cached,
        },
    }


def _endpoints(*native_ids: str) -> dict[str, Any]:
    rows = []
    for native_id in native_ids:
        _canonical, domain = near_ai._VERIFIED_DIRECT_MODELS[native_id]
        rows.append({"domain": domain, "models": [native_id]})
    return {"endpoints": rows}


def test_near_ai_fetch_intersects_catalog_direct_registry_and_release_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsv4 = "deepseek-ai/DeepSeek-V4-Flash"
    glm = "z-ai/glm-5.2"
    catalog = {
        "data": [
            _catalog_row(dsv4),
            _catalog_row(glm, prompt="0.0000014", completion="0.0000044"),
            _catalog_row("anthropic/claude-opus-5"),
            _catalog_row("unreviewed/new-model"),
        ]
    }
    endpoint_payload = _endpoints(dsv4, glm)

    class FakeResponse:
        def __init__(self, payload: object) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return copy.deepcopy(self.payload)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse(catalog if url == near_ai.CATALOG_URL else endpoint_payload)

    monkeypatch.setenv("NEAR_API_KEY", "test-key")
    monkeypatch.setattr(near_ai.httpx, "Client", FakeClient)
    result = near_ai.fetch()

    assert set(result.prices) == {"deepseek/deepseek-v4-flash", "z-ai/glm-5.2"}
    assert result.prices["deepseek/deepseek-v4-flash"].prompt_micro_per_m == 170_000
    assert result.prices["deepseek/deepseek-v4-flash"].completion_micro_per_m == 350_000
    assert result.prices["deepseek/deepseek-v4-flash"].tiers[0].prompt_cached_micro_per_m == 35_000
    assert near_ai._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.2"]["upstream_id"] == glm
    assert "anthropic/claude-opus-5" not in near_ai._DISCOVERED_MANIFEST_ROWS
    assert "unreviewed/new-model" not in near_ai._DISCOVERED_MANIFEST_ROWS


def test_near_ai_fetch_fails_closed_on_direct_domain_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glm = "z-ai/glm-5.2"

    class FakeResponse:
        def __init__(self, payload: object) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return copy.deepcopy(self.payload)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, url: str, **_kwargs: object) -> FakeResponse:
            if url == near_ai.CATALOG_URL:
                return FakeResponse({"data": [_catalog_row(glm)]})
            return FakeResponse(
                {"endpoints": [{"domain": "wrong.completions.near.ai", "models": [glm]}]}
            )

    monkeypatch.setenv("NEAR_API_KEY", "test-key")
    monkeypatch.setattr(near_ai.httpx, "Client", FakeClient)
    with pytest.raises(RuntimeError, match="empty pricing dict|missing expected models"):
        near_ai.fetch()


def test_near_ai_parser_rejects_bad_money_and_untrusted_endpoint_shapes() -> None:
    assert near_ai._microdollars_per_million_from_per_token("0.00000017") == 170_000
    assert near_ai._microdollars_per_million_from_per_token("NaN") is None
    assert near_ai._microdollars_per_million_from_per_token("-1") is None
    with pytest.raises(RuntimeError, match="unexpected shape"):
        near_ai._direct_domains({"endpoints": "not-a-list"})
    assert (
        near_ai._direct_domains(
            {"endpoints": [{"domain": "ATTACKER.completions.near.ai", "models": ["z-ai/glm-5.2"]}]}
        )
        == {}
    )


def test_near_ai_manifest_and_catalog_are_attested_prepaid_only() -> None:
    provider = PROVIDERS["near-ai"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is None
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True
    assert provider.provider_headquarters_country == "US"

    raw = json.loads(near_ai.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_ids = {row["id"] for row in raw["models"]}
    assert manifest_ids == {
        canonical for canonical, _domain in near_ai._VERIFIED_DIRECT_MODELS.values()
    }
    endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "near-ai"
    ]
    assert endpoints
    assert {endpoint.model_id for endpoint in endpoints} == manifest_ids
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    assert all(not endpoint.is_byok for endpoint in endpoints)
    assert all(
        endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
        for endpoint in endpoints
    )
    assert all(
        not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
        for endpoint in endpoints
    )
    assert all(
        not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_NO_STORE)
        for endpoint in endpoints
    )
    assert all(endpoint_stores_content(endpoint) for endpoint in endpoints)


def test_near_ai_is_e2e_eligible_but_never_satisfies_zdr_or_deny() -> None:
    e2e_ids = {model.id for model in e2e_candidate_models(limit=100)}
    assert "z-ai/glm-5.2" in e2e_ids

    settings = Settings(environment="test")
    e2e = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-5.2",
            "provider": {"only": ["near-ai"], "min_privacy": "e2e"},
        },
        settings,
    )
    assert {endpoint.provider for _model, endpoint in e2e} == {"near-ai"}

    for provider_filter in (
        {"only": ["near-ai"], "min_privacy": "zdr"},
        {"only": ["near-ai"], "data_collection": "deny", "min_privacy": "e2e"},
    ):
        with pytest.raises(HTTPException) as exc_info:
            chat_route_endpoint_candidates(
                {"model": "z-ai/glm-5.2", "provider": provider_filter},
                settings,
            )
        assert getattr(exc_info.value, "status_code", None) == 400


def test_near_ai_hourly_refresh_secret_and_authority_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names, normalize)
        for slug, url, env_names, normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    url, env_names, normalize = discoverable["near-ai"]
    assert url == near_ai.CATALOG_URL
    assert env_names == ("NEAR_API_KEY",)
    assert normalize("z-ai/glm-5.2") == "z-ai/glm-5.2"
    assert normalize("unreviewed/model") is None
    assert "near_ai" in PROVIDER_SLUGS
    assert "near-ai" in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert 'ensure_secret_from_env_file "NEAR_API_KEY" "trustedrouter-near-ai-api-key"' in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-near-ai-api-key"' in secrets
    assert "NEAR_API_KEY:trustedrouter-near-ai-api-key" in workflow
