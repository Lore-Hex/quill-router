from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.providers import _direct_openai, redpill
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
from trusted_router.services.inference_errors import default_provider_secret_ref


def test_redpill_provider_page_uses_official_branding(client: TestClient) -> None:
    response = client.get("/providers/redpill")
    assert response.status_code == 200
    assert "/static/provider-logos/redpill.png" in response.text
    assert "https://trustedrouter.com/static/og/providers/redpill.png" in response.text
    assert "z-ai/glm-5.3" in response.text
    assert "pending verified transport" in response.text


def test_redpill_has_separate_credits_identity_and_fails_closed_for_privacy() -> None:
    provider = PROVIDERS["redpill"]
    assert provider.supports_prepaid
    assert not provider.supports_byok
    assert not provider.provider_e2ee
    assert not provider.provider_confidential_compute
    assert not provider.provider_zero_data_retention
    assert OPENAI_COMPATIBLE_PROVIDERS["redpill"] == (
        ("REDPILL_API_KEY",), "https://api.redpill.ai/v1"
    )
    assert default_provider_secret_ref("redpill") == "env://REDPILL_API_KEY"
    endpoints = [ep for ep in MODEL_ENDPOINTS.values() if ep.provider == "redpill"]
    assert endpoints
    assert all(ep.usage_type == "Credits" for ep in endpoints)
    assert {ep.upstream_id for ep in endpoints} >= {
        "openai/gpt-oss-120b", "z-ai/glm-5.3", "z-ai/glm-5.3-flash"
    }
    for endpoint in endpoints:
        assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
        assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)


def _catalog(payload: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("REDPILL_API_KEY", "test-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_args, **_kw: payload)
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **_kw: True)
    return DirectOpenAIProvider(redpill.CATALOG.spec, manifest_path=tmp_path / "redpill.json")


def test_redpill_catalog_discovers_future_models_without_inheriting_tee_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    catalog = _catalog({"data": [{
        "id": "z-ai/glm-5.4", "name": "GLM 5.4", "is_tee": True,
        "providers": ["phala"], "context_length": 1_048_576,
        "input_modalities": ["text", "image"], "output_modalities": ["text"],
        "pricing": {"prompt": "0.0000014", "completion": "0.0000044",
                    "input_cache_read": "0.00000014"},
    }]}, monkeypatch, tmp_path)
    result = catalog.fetch()
    rows = catalog.discovered_rows
    price = result.prices["z-ai/glm-5.4"]
    assert price.prompt_micro_per_m == 1_400_000
    assert price.completion_micro_per_m == 4_400_000
    assert price.tiers[0].prompt_cached_micro_per_m == 140_000
    assert rows["z-ai/glm-5.4"]["upstream_id"] == "z-ai/glm-5.4"
    assert rows["z-ai/glm-5.4"]["context_length"] == 1_048_576
    assert "is_tee" not in rows["z-ai/glm-5.4"]
    assert "provider_e2ee" not in rows["z-ai/glm-5.4"]


def test_redpill_failed_canary_stays_dark_and_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    catalog = _catalog({"data": [{
        "id": "z-ai/glm-5.3", "pricing": {"prompt": "0.0000014", "completion": "0.0000044"},
    }]}, monkeypatch, tmp_path)
    canaries = []

    def probe(**kwargs):
        canaries.append(kwargs)
        return False

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    result = catalog.fetch()
    assert canaries[0]["require_message"] is True
    assert canaries[0]["api_key"] == "test-key"
    assert canaries[0]["model"] == "z-ai/glm-5.3"
    assert catalog.discovered_rows["z-ai/glm-5.3"]["routable"] is False
    catalog.write_provider_manifest(result)
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **_kw: True)
    result = catalog.fetch()
    assert catalog.discovered_rows["z-ai/glm-5.3"]["routable"] is True


def test_redpill_canary_rejects_200_without_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from scripts.pricing.openai_catalog import probe_openai_chat

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: httpx.Response(200, json={"choices": []}))
    assert not probe_openai_chat(
        base_url=redpill.BASE_URL, api_key="test", model="z-ai/glm-5.3", require_message=True,
    )


@pytest.mark.parametrize("model,field", [
    ("openai/gpt-5.6-luna", "max_completion_tokens"),
    ("openai/gpt-6-astra", "max_completion_tokens"),
    ("openai/o3", "max_completion_tokens"),
    ("openai/gpt-oss-120b", "max_tokens"),
    ("z-ai/glm-5.3", "max_tokens"),
])
def test_redpill_canary_uses_the_model_token_contract(
    model: str, field: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    catalog = _catalog({"data": [{
        "id": model, "pricing": {"prompt": "0.000001", "completion": "0.000004"},
    }]}, monkeypatch, tmp_path)
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    catalog.fetch()
    assert calls[0]["max_tokens_field"] == field


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": "bad"}, {"data": []}])
def test_redpill_rejects_empty_or_invalid_catalog(
    payload: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    catalog = _catalog(payload, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="catalog|priced"):
        catalog.fetch()


def test_redpill_requires_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    redpill = importlib.import_module("scripts.pricing.providers.redpill")
    monkeypatch.delenv("REDPILL_API_KEY", raising=False)
    monkeypatch.setenv("PHALA_API_KEY", "not-the-redpill-key")
    with pytest.raises(RuntimeError, match="REDPILL_API_KEY"):
        redpill.fetch()


def test_redpill_hourly_refresh_and_secret_are_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text()
    secrets = (root / "scripts/deploy/secrets.sh").read_text()
    assert "REDPILL_API_KEY:trustedrouter-redpill-api-key" in workflow
    assert '"REDPILL_API_KEY" "trustedrouter-redpill-api-key"' in secrets
    assert '"PHALA_API_KEY" "trustedrouter-phala-api-key" "REDPILL_API_KEY"' not in secrets
