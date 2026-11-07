from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import featherless, jina, scaleway
from tests.pinned_manifests import (
    FEATHERLESS_DEEPSEEK_V4_1_FLASH,
    FEATHERLESS_QWEN38_FLASH_NEXT,
    build_manifest_rows,
)
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS, providers_for_display
from trusted_router.provider_manifest_policy import (
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
)

_SCALEWAY_CATALOG = Path(__file__).parent / "fixtures/pricing/scaleway_product_catalog_2026-11-07.json"


def _scaleway_product(
    product: str, token_type: str, nanos: int, *, mode: str = "realtime", region: str = "fr-par",
) -> dict[str, Any]:
    return {
        "sku": f"/ai/generative_apis/consumption/{product}_{token_type}_{mode}/{region}",
        "product_category": "Generative APIs",
        "product": product,
        "locality": {"region": region},
        "price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": nanos}},
        "properties": {"generative_apis": {"consumption_mode": mode, "token_type": token_type}},
        "unit_of_measure": {"unit": "token", "size": 1000},
        "status": "general_availability",
    }


def test_scaleway_converts_first_party_eur_prices_with_fx_reserve() -> None:
    products = [
        _scaleway_product("glm-5.2", "input_token", 1_800_000),
        _scaleway_product("glm-5.2", "input_cached_token", 300_000),
        _scaleway_product("glm-5.2", "output_token", 5_500_000),
        # Batch and other-region rows are different tariffs and are ignored.
        _scaleway_product("glm-5.2", "input_token", 900_000, mode="batch"),
        _scaleway_product("glm-5.2", "output_token", 1, region="nl-ams"),
    ]
    assert scaleway._usd_per_eur("<Cube currency='USD' rate='1.20'/>") == Decimal("1.20")
    assert scaleway._parse_catalog_prices(products, usd_per_eur=Decimal("1.20")) == {
        "z-ai/glm-5.2": ModelPrice(
            2_268_000,
            6_930_000,
            prompt_cached_micro_per_m=378_000,
        )
    }


def test_scaleway_public_product_catalog_fixture_prices_every_mapped_model() -> None:
    # 2026-11-07: www.scaleway.com/en/pricing/model-as-a-service/ returned 403
    # to the refresh; the public Product Catalog API carries the same tariffs.
    payload = json.loads(_SCALEWAY_CATALOG.read_text(encoding="utf-8"))
    assert payload["total_count"] == len(payload["products"])

    prices = scaleway._parse_catalog_prices(payload["products"], usd_per_eur=Decimal("1.20"))

    assert set(prices) == set(scaleway.MODEL_MAP.values())
    # EUR 0.40 / 0.08 / 0.80 per million x 1.20 USD/EUR x the 5% FX reserve.
    assert prices["deepseek/deepseek-v4-flash-0731"] == ModelPrice(
        504_000, 1_008_000, prompt_cached_micro_per_m=100_800,
    )
    assert prices["z-ai/glm-5.2"] == ModelPrice(2_268_000, 6_930_000)
    assert prices["openai/gpt-oss-120b"] == ModelPrice(189_000, 756_000)


def test_scaleway_catalog_pages_until_total_count(monkeypatch: pytest.MonkeyPatch) -> None:
    products = json.loads(_SCALEWAY_CATALOG.read_text(encoding="utf-8"))["products"]
    urls: list[str] = []

    def fetch(url: str, **_kwargs: object) -> dict[str, Any]:
        urls.append(url)
        page = int(url.rsplit("page=", 1)[1])
        return {"products": products[(page - 1) * 100 : page * 100], "total_count": len(products)}

    monkeypatch.setattr(scaleway, "fetch_json", fetch)

    assert scaleway._fetch_catalog_products() == products
    assert len(urls) == 2
    assert all(url.startswith(scaleway.PRICING_URL + "?product_types=generative_apis") for url in urls)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"products": [], "total_count": None},
        {"products": [], "total_count": 5},
    ],
)
def test_scaleway_catalog_fails_closed_on_reshaped_or_truncated_payload(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(scaleway, "fetch_json", lambda _url, **_kwargs: payload)

    with pytest.raises(RuntimeError, match="scaleway: product catalog"):
        scaleway._fetch_catalog_products()


@pytest.mark.parametrize(
    "change",
    [
        {"unit_of_measure": {"unit": "second", "size": 60}},
        {"price": {"retail_price": {"currency_code": "USD", "units": 0, "nanos": 1}}},
        {"price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": "1"}}},
    ],
)
def test_scaleway_catalog_rejects_unexpected_price_units(change: dict[str, Any]) -> None:
    products = [
        {**_scaleway_product("glm-5.2", "input_token", 1_800_000), **change},
        _scaleway_product("glm-5.2", "output_token", 5_500_000),
    ]

    with pytest.raises(RuntimeError, match="scaleway: "):
        scaleway._parse_catalog_prices(products, usd_per_eur=Decimal("1.20"))


def test_scaleway_catalog_rejects_conflicting_duplicate_tariffs() -> None:
    products = [
        _scaleway_product("glm-5.2", "input_token", 1_800_000),
        _scaleway_product("glm-5.2", "input_token", 1_900_000),
        _scaleway_product("glm-5.2", "output_token", 5_500_000),
    ]

    with pytest.raises(RuntimeError, match="conflicting prompt prices"):
        scaleway._parse_catalog_prices(products, usd_per_eur=Decimal("1.20"))


def test_scaleway_rejects_missing_fx_rate() -> None:
    with pytest.raises(RuntimeError, match="no USD/EUR rate"):
        scaleway._usd_per_eur("<Cube currency='JPY' rate='170'/>")


def test_featherless_uses_shared_canonical_model_ids() -> None:
    assert (
        featherless.CATALOG.model_id("deepseek-ai/DeepSeek-V4.1-Flash")
        == "deepseek/deepseek-v4.1-flash"
    )
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
        "deepseek-ai/DeepSeek-V4.1-Flash",
        "Qwen/Qwen3.8-Flash-Next",
        "zai-org/GLM-5.3",
    } <= set(featherless.CURATED_NATIVE_MODELS)


def test_featherless_deepseek_v41_route_preserves_provider_prices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A row the refresh tombstoned is dark; a live one is routed at its exact
    # upstream ID and the provider's prices plus markup. The row is pinned as
    # Featherless listed it on 2026-09-30; what it lists today is
    # test_featherless_lists_deepseek_v41_flash_with_its_limits_and_cache_price.
    _models, endpoints = build_manifest_rows(
        monkeypatch, tmp_path, "featherless", [FEATHERLESS_DEEPSEEK_V4_1_FLASH]
    )
    endpoint = endpoints["deepseek/deepseek-v4.1-flash@featherless/prepaid"]
    assert endpoint.upstream_id == "deepseek-ai/DeepSeek-V4.1-Flash"
    assert endpoint.published_prompt_price_microdollars_per_million_tokens == 316_500
    assert endpoint.published_completion_price_microdollars_per_million_tokens == 1_266_000
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 31_650

    tombstoned = {
        **FEATHERLESS_DEEPSEEK_V4_1_FLASH,
        "routable": False,
        "routable_reason": "delisted-upstream",
    }
    _models, endpoints = build_manifest_rows(
        monkeypatch, tmp_path, "featherless", [tombstoned, FEATHERLESS_QWEN38_FLASH_NEXT]
    )
    assert "deepseek/deepseek-v4.1-flash@featherless/prepaid" not in endpoints
    assert "qwen/qwen3.8-flash-next@featherless/prepaid" in endpoints


@pytest.mark.provider_health
def test_featherless_lists_deepseek_v41_flash_with_its_limits_and_cache_price() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    manifest = json.loads(featherless.MANIFEST_PATH.read_text())
    row = next(row for row in manifest["models"] if row["id"] == "deepseek/deepseek-v4.1-flash")
    assert row["context_length"] == 262144
    # Featherless's pricing page stopped publishing cache rates; the refresh
    # records one only when published, and never above the input price.
    cached = row.get("cached_input_token_price_per_m")
    assert cached is None or cached <= row["input_token_price_per_m"]
    assert row["max_output_tokens"] == 32768
    assert row["input_modalities"] == ["text", "image"]


@pytest.mark.provider_health
def test_featherless_serves_deepseek_v41_flash() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert "deepseek/deepseek-v4.1-flash@featherless/prepaid" in MODEL_ENDPOINTS


@pytest.mark.provider_health
def test_featherless_qwen38_flash_next_is_routable() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert "qwen/qwen3.8-flash-next@featherless/prepaid" in MODEL_ENDPOINTS


def test_featherless_routes_qwen38_flash_next_at_its_native_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Featherless dispatches on its own id, case and all, not the canonical
    # one, and the route bills its price plus markup.
    _models, endpoints = build_manifest_rows(
        monkeypatch, tmp_path, "featherless", [FEATHERLESS_QWEN38_FLASH_NEXT]
    )
    endpoint = endpoints["qwen/qwen3.8-flash-next@featherless/prepaid"]

    assert endpoint.provider == "featherless"
    assert endpoint.upstream_id == "Qwen/Qwen3.8-Flash-Next"
    assert endpoint.usage_type == "Credits"
    assert endpoint.published_prompt_price_microdollars_per_million_tokens == 158_250
    assert endpoint.published_completion_price_microdollars_per_million_tokens == 527_500


def test_featherless_discovers_only_first_party_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "featherless.json"
    manifest.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(featherless.CATALOG, "manifest_path", manifest)
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
