from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.base import ProviderPricingResult
from scripts.pricing.providers import zero_g
from scripts.pricing.refresh import (
    _PRICING_RESULT_PROVIDER_ALIASES,
    PROVIDER_SLUGS,
)
from trusted_router import catalog_ingest
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PRIVACY_TIER_CONFIDENTIAL,
    Model,
)
from trusted_router.catalog_privacy import (
    endpoint_e2ee,
    endpoint_privacy_tier,
    provider_privacy_tier,
)
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS, ProviderClient
from trusted_router.services.inference_errors import default_provider_secret_ref


def _model(
    model_id: str,
    *,
    model_type: str = "chatbot",
    prompt: str = "0.00000008",
    completion: str = "0.00000048",
    cached: str | None = "0.00000002",
    parameters: list[str] | None = None,
) -> dict[str, Any]:
    pricing = {"prompt": prompt, "completion": completion}
    if cached is not None:
        pricing["cached_prompt"] = cached
    return {
        "id": model_id,
        "object": "model",
        "owned_by": "0G Foundation",
        "name": model_id,
        "type": model_type,
        "context_length": 1_048_576,
        "max_completion_tokens": 131_072,
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": parameters or ["tools", "response_format", "reasoning_effort"],
        "pricing_usd": pricing,
        # The authenticated account can see upstream attestation metadata, but
        # TrustedRouter deliberately does not promote it to a product claim.
        "verifiability": "TeeML",
        "tee_attested": True,
        "provider_count": 3,
    }


def _catalog_payload() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            _model("0gm-1.0-35b-a3b"),
            _model("claude-opus-4-8", prompt="0.0000045", completion="0.0000225"),
            _model("glm-5.2", prompt="0.0000009", completion="0.000003"),
            _model("kimi-k3", prompt="0.000003", completion="0.000015"),
            _model("minimax-m3", prompt="0.00000027", completion="0.00000108"),
            _model("gpt-5.6-sol", prompt="0.0000045", completion="0.000027"),
            _model("qwen3-vl-30b", prompt="0.0000000193", completion="0.0000001892"),
            _model("qwen3.7-plus", prompt="0.0000002208", completion="0.0000008808"),
            _model("z-image-turbo", model_type="text-to-image"),
            _model("whisper-large-v3", model_type="speech-to-text"),
        ],
    }


def test_zero_g_parser_admits_every_priced_chat_model_as_standard() -> None:
    prices, rows = zero_g.parse_catalog(_catalog_payload())

    assert set(rows) == {
        "zero-g/0gm-1.0-35b-a3b",
        "anthropic/claude-opus-4.8",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k3",
        "minimax/minimax-m3",
        "openai/gpt-5.6-sol",
        "qwen/qwen3-vl-30b-a3b-instruct",
        "qwen/qwen3.7-plus",
    }
    assert set(prices) == set(rows)
    assert all(row["trust_mode"] == "standard" for row in rows.values())
    assert all(row["verifiability"] is None for row in rows.values())
    assert all(row["tee_attested"] is False for row in rows.values())
    assert all("private_inference" not in row["supported_features"] for row in rows.values())
    assert all("teeml" not in row["supported_features"] for row in rows.values())

    glm = prices["z-ai/glm-5.2"]
    assert glm.prompt_micro_per_m == 900_000
    assert glm.completion_micro_per_m == 3_000_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 20_000
    assert rows["z-ai/glm-5.2"]["provider_route_count"] == 3
    assert rows["z-ai/glm-5.2"]["supported_features"] == [
        "chat",
        "completion",
        "tools",
        "json_mode",
        "structured_outputs",
        "reasoning",
        "prompt_caching",
    ]


def test_zero_g_parser_excludes_non_chat_and_unpriced_models() -> None:
    payload = _catalog_payload()
    payload["data"].append(_model("price-missing"))
    payload["data"][-1].pop("pricing_usd")

    _prices, rows = zero_g.parse_catalog(payload)

    assert "zero-g/z-image-turbo" not in rows
    assert "zero-g/whisper-large-v3" not in rows
    assert all("price-missing" not in model_id for model_id in rows)


@pytest.mark.parametrize("payload", [None, {}, {"data": {}}, {"data": "models"}])
def test_zero_g_parser_rejects_malformed_catalog(payload: object) -> None:
    with pytest.raises(RuntimeError, match="data list"):
        zero_g.parse_catalog(payload)


def test_zero_g_future_model_families_normalize_without_allowlists() -> None:
    assert zero_g._canonical_model_id("claude-opus-5") == "anthropic/claude-opus-5"
    assert zero_g._canonical_model_id("gpt-5.6-sol") == "openai/gpt-5.6-sol"
    assert zero_g._canonical_model_id("kimi-k3") == "moonshotai/kimi-k3"
    assert zero_g._canonical_model_id("qwen3.7-plus") == "qwen/qwen3.7-plus"


def test_zero_g_fetch_uses_all_model_key_and_standard_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_call: dict[str, Any] = {}
    canary_call: dict[str, Any] = {}

    def fetch_json(url: str, **kwargs: Any) -> dict[str, Any]:
        catalog_call["url"] = url
        catalog_call.update(kwargs)
        return _catalog_payload()

    def probe(**kwargs: Any) -> bool:
        canary_call.update(kwargs)
        return True

    monkeypatch.setenv("ZERO_G_ALL_API_KEY", "all-model-key")
    monkeypatch.setattr(zero_g, "EXPECTED_MODELS", ["zero-g/0gm-1.0-35b-a3b"])
    monkeypatch.setattr(zero_g, "fetch_json", fetch_json)
    monkeypatch.setattr(zero_g, "probe_openai_chat", probe)
    monkeypatch.setattr(zero_g, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(zero_g, "_LIVE_CANARY_OK", False)

    result = zero_g.fetch()

    assert result.slug == "zero-g"
    assert catalog_call == {
        "url": "https://router-api.0g.ai/v1/models",
        "extra_headers": {"Authorization": "Bearer all-model-key"},
    }
    assert canary_call == {
        "base_url": "https://router-api.0g.ai/v1",
        "api_key": "all-model-key",
        "model": "0gm-1.0-35b-a3b",
        "expected_content": "PONG",
        "max_tokens": 256,
    }
    assert "extra_headers" not in canary_call
    assert zero_g._LIVE_CANARY_OK is True
    assert all(row["routable"] is True for row in zero_g._DISCOVERED_MANIFEST_ROWS.values())


def test_zero_g_fetch_fails_closed_without_all_model_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZERO_G_ALL_API_KEY", raising=False)
    monkeypatch.setenv("ZERO_G_API_KEY", "legacy-key-must-not-be-used")

    with pytest.raises(RuntimeError, match="ZERO_G_ALL_API_KEY"):
        zero_g.fetch()


def test_zero_g_manifest_writer_preserves_dark_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices, rows = zero_g.parse_catalog(_catalog_payload())
    manifest_path = tmp_path / "zero-g.json"
    manifest_path.write_text(
        json.dumps(
            {
                "_about": "legacy private TeeML catalog",
                "provider": "zero-g",
                "models": [
                    {
                        "id": model_id,
                        "tee_type": "TDX",
                        "tee_verifier": "dstack",
                        "private_provider_count": 1,
                        "routable": False,
                        "routable_reason": "provider-canary-failed",
                    }
                    for model_id in rows
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(zero_g, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(zero_g, "_DISCOVERED_MANIFEST_ROWS", rows)
    monkeypatch.setattr(zero_g, "_LIVE_CANARY_OK", False)
    result = ProviderPricingResult(
        slug="zero-g",
        prices=prices,
        source="api",
        fetched_url=zero_g.URL,
    )

    zero_g.write_provider_manifest(result)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model_count"] == 8
    assert "unrestricted 0G router catalog" in manifest["_about"]
    assert "no ZDR" in manifest["_about"]
    assert all(not (zero_g._LEGACY_PRIVATE_FIELDS & row.keys()) for row in manifest["models"])
    assert all(
        row["routable"] is False and row["routable_reason"] == "provider-canary-failed"
        for row in manifest["models"]
    )


def test_zero_g_catalog_and_local_adapter_make_no_privacy_claim() -> None:
    provider = PROVIDERS["zero-g"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert provider_privacy_tier(provider) != PRIVACY_TIER_CONFIDENTIAL
    assert "unrestricted standard router catalog" in provider.provider_policy
    assert "not classified as ZDR" in provider.provider_policy
    assert "zero-g" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS["zero-g"] == (
        ("ZERO_G_ALL_API_KEY",),
        "https://router-api.0g.ai/v1",
    )
    model = Model(
        id="z-ai/glm-5.2",
        name="GLM 5.2",
        provider="zero-g",
        context_length=1_048_576,
        upstream_id="glm-5.2",
    )
    assert ProviderClient._provider_extra_headers(model) == {}
    assert default_provider_secret_ref("zero-g") == "env://ZERO_G_ALL_API_KEY"

    manifest_path = (
        Path(__file__).resolve().parents[1] / "src/trusted_router/data/provider_models/zero-g.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    routable_rows = {row["id"]: row for row in manifest["models"] if row.get("routable") is True}
    zero_g_endpoints = {
        endpoint.model_id: endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "zero-g"
    }

    assert zero_g_endpoints.keys() == routable_rows.keys()
    assert all(
        row["trust_mode"] == "standard"
        and row["verifiability"] is None
        and row["tee_attested"] is False
        and not (zero_g._LEGACY_PRIVATE_FIELDS & row.keys())
        for row in routable_rows.values()
    )
    assert all(
        endpoint.usage_type == "Credits"
        and endpoint.upstream_id == routable_rows[model_id]["upstream_id"]
        for model_id, endpoint in zero_g_endpoints.items()
    )
    assert all(
        endpoint_e2ee(endpoint) is False
        and endpoint_privacy_tier(endpoint) != PRIVACY_TIER_CONFIDENTIAL
        for endpoint in zero_g_endpoints.values()
    )


def test_zero_g_activated_manifest_imports_prepaid_route_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "provider": "zero-g",
        "price_scale": "microdollars_per_million",
        "models": [
            {
                "id": "zero-g/test-standard-model",
                "upstream_id": "test-standard-model",
                "display_name": "Test Standard Model",
                "model_type": "chat",
                "endpoints": ["chat/completions"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "context_length": 131_072,
                "routable": True,
                "trust_mode": "standard",
                "verifiability": None,
                "tee_attested": False,
                "input_token_price_per_m": 80_000,
                "output_token_price_per_m": 480_000,
            }
        ],
    }
    (tmp_path / "zero-g.json").write_text(
        json.dumps(manifest) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()

    assert set(models) == {"zero-g/test-standard-model"}
    assert set(endpoints) == {"zero-g/test-standard-model@zero-g/prepaid"}
    endpoint = endpoints["zero-g/test-standard-model@zero-g/prepaid"]
    assert endpoint.provider == "zero-g"
    assert endpoint.usage_type == "Credits"
    assert endpoint.upstream_id == "test-standard-model"
    assert endpoint_e2ee(endpoint) is False
    assert endpoint_privacy_tier(endpoint) != PRIVACY_TIER_CONFIDENTIAL


def test_zero_g_hourly_refresh_and_secret_wiring_are_complete() -> None:
    assert "zero_g" in PROVIDER_SLUGS
    assert _PRICING_RESULT_PROVIDER_ALIASES["zero_g"] == ("zero-g",)

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    rollout = (root / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert (
        'ensure_secret_from_env_file "ZERO_G_ALL_API_KEY" "trustedrouter-zero-g-api-key"'
    ) in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-zero-g-api-key"' in secrets
    assert (
        'add_secret_env_if_exists "ZERO_G_ALL_API_KEY" "trustedrouter-zero-g-api-key"'
    ) in rollout
    assert "Pull optional unrestricted 0G router key" in workflow
    assert "ZERO_G_ALL_API_KEY=${KEY}" in workflow


def test_zero_g_public_provider_page_is_published(client: Any) -> None:
    page = client.get("/providers/zero-g")
    assert page.status_code == 200
    assert "0G" in page.text
    assert "unrestricted standard router catalog" in page.text
    assert "end-to-end encrypted" in page.text

    providers = {row["id"]: row for row in client.get("/v1/providers").json()["data"]}
    provider = providers["zero-g"]
    assert provider["supports_prepaid"] is True
    assert provider["supports_byok"] is False
    assert provider["provider_zero_data_retention"] is False
    assert provider["provider_confidential_compute"] is False
    assert provider["provider_e2ee"] is False

    sitemap = client.get("/sitemap-providers.xml")
    assert sitemap.status_code == 200
    assert "https://trustedrouter.com/providers/zero-g" in sitemap.text
