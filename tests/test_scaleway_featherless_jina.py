from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import featherless, jina, scaleway
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS, providers_for_display
from trusted_router.provider_manifest_policy import (
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
    RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS,
)


def test_scaleway_converts_first_party_eur_prices_with_fx_reserve() -> None:
    html = """
    <table><tbody><tr>
      <td>glm-5.2</td><td>Chat and code</td>
      <td>€1.80 / million tokens<br>€0.30 / million tokens cached</td>
      <td>€5.50 / million tokens</td>
    </tr></tbody></table>
    """
    assert scaleway._usd_per_eur("<Cube currency='USD' rate='1.20'/>") == Decimal("1.20")
    assert scaleway._parse_prices(html, usd_per_eur=Decimal("1.20")) == {
        "z-ai/glm-5.2": ModelPrice(
            2_268_000,
            6_930_000,
            prompt_cached_micro_per_m=378_000,
        )
    }


def test_scaleway_rejects_missing_fx_rate() -> None:
    with pytest.raises(RuntimeError, match="no USD/EUR rate"):
        scaleway._usd_per_eur("<Cube currency='JPY' rate='170'/>")


def test_featherless_uses_shared_canonical_model_ids() -> None:
    assert (
        featherless.CATALOG.model_id("deepseek-ai/DeepSeek-V4-Flash-0731")
        == "deepseek/deepseek-v4-flash-0731"
    )
    assert featherless.CATALOG.model_id("zai-org/GLM-5.2") == "z-ai/glm-5.2"
    assert (
        featherless.CATALOG.model_id("zai-org/GLM-5.3-Flash")
        == "z-ai/glm-5.3-flash"
    )
    assert featherless.CATALOG.model_id("zai-org/GLM-5.3") == "z-ai/glm-5.3"
    assert featherless.CATALOG.model_id("moonshotai/Kimi-K3") == "moonshotai/kimi-k3"
    assert (
        featherless.CATALOG.model_id("Qwen/Qwen3.8-Flash-Next")
        == "qwen/qwen3.8-flash-next"
    )
    assert {
        "Qwen/Qwen3.8-Flash-Next",
        "zai-org/GLM-5.3",
    } <= set(featherless.CURATED_NATIVE_MODELS)


def test_featherless_qwen38_flash_next_is_routable() -> None:
    endpoint = MODEL_ENDPOINTS["qwen/qwen3.8-flash-next@featherless/prepaid"]

    assert endpoint.provider == "featherless"
    assert endpoint.upstream_id == "Qwen/Qwen3.8-Flash-Next"
    assert endpoint.usage_type == "Credits"
    assert endpoint.published_prompt_price_microdollars_per_million_tokens == 158_250
    assert endpoint.published_completion_price_microdollars_per_million_tokens == 527_500


def test_featherless_discovers_only_first_party_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = {
        "data": [
            {
                "id": "Qwen/Qwen3.8-27B",
                "available_on_current_plan": True,
            },
            {
                "id": "community/Qwen3.8-27B-fine-tune",
                "available_on_current_plan": True,
            },
            {
                "id": "zai-org/future-model",
                "available_on_current_plan": False,
            },
        ]
    }
    required = {
        native_id: {
            "id": native_id,
            "available_on_current_plan": True,
        }
        for native_id in featherless.CURATED_NATIVE_MODELS
    }
    requested_urls: list[str] = []

    def fake_fetch(url: str, **_kwargs: Any) -> dict[str, Any]:
        requested_urls.append(url)
        if "?" in url:
            return listing
        native_id = next(
            native_id
            for native_id in featherless.CURATED_NATIVE_MODELS
            if url.endswith(native_id.replace("/", "%2F"))
        )
        return required[native_id]

    monkeypatch.setattr(featherless, "fetch_json", fake_fetch)

    rows = featherless._load_rows("test-key")

    assert {row["id"] for row in rows} == {
        *featherless.CURATED_NATIVE_MODELS,
        "Qwen/Qwen3.8-27B",
    }
    assert "community/Qwen3.8-27B-fine-tune" not in {row["id"] for row in rows}
    assert "available_on_current_plan=true" in requested_urls[0]
    assert f"per_page={featherless.DISCOVERY_PAGE_SIZE}" in requested_urls[0]


def test_jina_discovers_only_priced_embedding_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "data": [
            {
                "id": "jina-ai/jina-embeddings-v5-text-nano",
                "name": "Jina Embeddings v5 Text Nano",
                "context_length": 32768,
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
                "pricing": {"prompt": "0.00000002", "completion": "0"},
            },
            {
                "id": "jina-ai/jina-embeddings-v5-text-small",
                "name": "Jina Embeddings v5 Text Small",
                "context_length": 32768,
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
                "pricing": {"prompt": "0.00000005", "completion": "0"},
            },
            {
                "id": "jina-ai/jina-reranker-v3",
                "output_modalities": ["scores"],
                "pricing": {"prompt": "0.00000002", "completion": "0"},
            },
        ]
    }

    class Response:
        status_code = 200

    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    monkeypatch.setattr(jina, "fetch_json", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(jina.httpx, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(jina, "MANIFEST_PATH", tmp_path / "jina.json")

    result = jina.fetch()
    assert result.include_in_price_index is False
    assert jina.INCLUDE_IN_PRICE_INDEX is False
    assert result.prices == {
        "jina-ai/jina-embeddings-v5-text-nano": ModelPrice(20_000, 0),
        "jina-ai/jina-embeddings-v5-text-small": ModelPrice(50_000, 0),
    }
    jina.write_provider_manifest(result)
    manifest = json.loads(jina.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert {row["model_type"] for row in manifest["models"]} == {"embedding"}
    assert {tuple(row["endpoints"]) for row in manifest["models"]} == {("embeddings",)}


def test_blocked_providers_remain_public_but_have_no_routes(client: TestClient) -> None:
    displayed = {provider.slug for provider in providers_for_display()}
    assert {"ovhcloud", "vultr"} <= displayed
    assert not any(
        endpoint.provider in {"ovhcloud", "vultr"}
        for endpoint in MODEL_ENDPOINTS.values()
    )
    for slug in ("ovhcloud", "vultr"):
        assert PROVIDERS[slug].supports_prepaid is False
        html = client.get(f"/providers/{slug}")
        assert html.status_code == 200
        assert "Not routable" in html.text

    payload = client.get("/providers", headers={"accept": "application/json"}).json()
    rows = {row["id"]: row for row in payload["data"]}
    assert rows["ovhcloud"]["routing_status"] == "blocked"
    assert rows["vultr"]["routing_status_reason"]


def test_new_provider_manifests_expire_without_hiding_ci_secret_failures() -> None:
    assert {"scaleway", "featherless", "jina"} <= EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert {"scaleway", "featherless", "jina"}.isdisjoint(
        RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS
    )
