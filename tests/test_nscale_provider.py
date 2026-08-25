from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, read_stale_provider_manifest
from scripts.pricing.manifest import apply_canary_results
from scripts.pricing.providers import nscale
from trusted_router import catalog_ingest
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
from trusted_router.services.inference_errors import default_provider_secret_ref


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "id": f"ExampleOrg/Chat-{index}",
            "context_length": 131_072,
            "pricing": {"input": 0.1, "output": 0.4},
        }
        for index in range(15)
    ]
    rows.extend(
        [
            {
                "id": nscale.EMBEDDING_UPSTREAM_ID,
                "context_length": 32_768,
                "pricing": {"input": 0.04, "output": 0},
            },
            {
                "id": nscale.IMAGE_UPSTREAM_ID,
                "pricing": {"input": 0, "output": 0.0013},
            },
        ]
    )
    return rows


def _encoded_png(size: tuple[int, int] = nscale.IMAGE_DIMENSIONS) -> str:
    output = BytesIO()
    Image.new("RGB", size, color="white").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def test_nscale_discovers_chat_embedding_and_image_with_exact_units() -> None:
    nscale.UPSTREAM_ID_MAP.clear()
    chat_prices, manifest_prices, rows = nscale._discover(_rows())

    assert len(chat_prices) == 15
    assert chat_prices["exampleorg/chat-0"] == ModelPrice(100_000, 400_000)
    assert manifest_prices[nscale.EMBEDDING_MODEL_ID] == ModelPrice(40_000, 0)
    assert manifest_prices[nscale.IMAGE_MODEL_ID] == ModelPrice(0, 0)
    assert rows[nscale.EMBEDDING_MODEL_ID]["model_type"] == "embedding"
    assert rows[nscale.EMBEDDING_MODEL_ID]["endpoints"] == ["embeddings"]
    assert rows[nscale.IMAGE_MODEL_ID]["model_type"] == "image"
    assert rows[nscale.IMAGE_MODEL_ID]["fixed_output_price_microdollars"] == {"1k": 1_364}
    assert nscale.UPSTREAM_ID_MAP[nscale.IMAGE_MODEL_ID] == nscale.IMAGE_UPSTREAM_ID


def test_nscale_chat_discovery_survives_independent_media_retirement() -> None:
    nscale.UPSTREAM_ID_MAP.clear()
    chat_prices, manifest_prices, rows = nscale._discover(_rows()[:15])

    assert len(chat_prices) == 15
    assert manifest_prices == chat_prices
    assert {row["model_type"] for row in rows.values()} == {"chat"}
    assert nscale.EMBEDDING_MODEL_ID not in rows
    assert nscale.IMAGE_MODEL_ID not in rows


def test_nscale_invalid_media_prices_are_quarantined_without_hiding_chat() -> None:
    rows = _rows()
    rows[-2]["pricing"] = {"input": 0, "output": 1}
    rows[-1]["pricing"] = {"input": 0, "output": "invalid"}

    chat_prices, manifest_prices, discovered = nscale._discover(rows)

    assert len(chat_prices) == 15
    assert nscale.EMBEDDING_MODEL_ID not in manifest_prices
    assert nscale.IMAGE_MODEL_ID not in manifest_prices
    assert discovered[nscale.EMBEDDING_MODEL_ID]["routable"] is False
    assert discovered[nscale.EMBEDDING_MODEL_ID]["routable_reason"] == "price-unavailable"
    assert discovered[nscale.IMAGE_MODEL_ID]["routable"] is False
    assert discovered[nscale.IMAGE_MODEL_ID]["routable_reason"] == "price-unavailable"


def test_nscale_canary_results_publish_only_verified_routes() -> None:
    _chat_prices, _manifest_prices, rows = nscale._discover(_rows())
    checked = set(rows)
    healthy = {"exampleorg/chat-0", nscale.IMAGE_MODEL_ID}

    apply_canary_results(
        rows,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )

    for model_id, row in rows.items():
        if model_id in healthy:
            assert row["routable"] is True
            assert "routable_reason" not in row
        else:
            assert row["routable"] is False
            assert row["routable_reason"] == "provider-canary-failed"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 1},
            },
            True,
        ),
        (
            {
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 0},
            },
            False,
        ),
        (
            {
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": True},
            },
            False,
        ),
        (
            {
                "data": [{"embedding": [0.1, False]}],
                "usage": {"prompt_tokens": 1},
            },
            False,
        ),
        ({"data": [], "usage": {"prompt_tokens": 1}}, False),
    ],
)
def test_nscale_embedding_canary_validates_metered_response(
    monkeypatch,
    payload: dict[str, object],
    expected: bool,
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        nscale.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(200, json=payload),
    )

    assert nscale._probe_embedding("key") is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"b64_json": _encoded_png()}]}, True),
        ({"data": [{"b64_json": _encoded_png((512, 512))}]}, False),
        (
            {
                "data": [
                    {"b64_json": _encoded_png()},
                    {"b64_json": _encoded_png()},
                ]
            },
            False,
        ),
        ({"data": [{"url": "https://example.invalid/image.png"}]}, False),
        ({"data": [{"b64_json": "not-base64"}]}, False),
        (
            {"data": [{"b64_json": base64.b64encode(b"not an image" * 20).decode("ascii")}]},
            False,
        ),
    ],
)
def test_nscale_image_canary_validates_one_exact_native_image(
    monkeypatch,
    payload: dict[str, object],
    expected: bool,
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        nscale.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(200, json=payload),
    )

    assert nscale._probe_image("key") is expected


def test_nscale_image_canary_sends_only_the_billed_native_size(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def post(*_args, **kwargs) -> httpx.Response:  # noqa: ANN002, ANN003
        captured.update(kwargs["json"])
        return httpx.Response(200, json={"data": [{"b64_json": _encoded_png()}]})

    monkeypatch.setattr(nscale.httpx, "post", post)

    assert nscale._probe_image("key") is True
    assert captured == {
        "model": nscale.IMAGE_UPSTREAM_ID,
        "prompt": "A single black square on a white background",
        "n": 1,
        "size": "1024x1024",
    }


def test_nscale_unknown_upstream_model_fails_canary_without_request(monkeypatch) -> None:
    nscale.UPSTREAM_ID_MAP.clear()

    def unexpected_request(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        raise AssertionError("missing upstream model must not make a request")

    monkeypatch.setattr(nscale, "probe_openai_chat", unexpected_request)

    assert nscale._probe("key", "vendor/missing") is False


def test_nscale_canaries_fail_closed_on_http_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        nscale.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(402, json={"error": "credit"}),
    )
    assert nscale._probe_embedding("key") is False
    assert nscale._probe_image("key") is False

    def raise_timeout(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(nscale.httpx, "post", raise_timeout)
    assert nscale._probe_embedding("key") is False
    assert nscale._probe_image("key") is False


def test_nscale_fixed_price_change_disables_route_until_enclave_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "nscale.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": nscale.IMAGE_MODEL_ID,
                        "fixed_output_price_microdollars": {"1k": 1_364},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(nscale, "MANIFEST_PATH", manifest)
    discovered = {
        nscale.IMAGE_MODEL_ID: {
            "fixed_output_price_microdollars": {"1k": 2_000},
            "routable": True,
        }
    }

    assert nscale._quarantine_fixed_price_changes(discovered) == 1
    row = discovered[nscale.IMAGE_MODEL_ID]
    assert row["fixed_output_price_microdollars"] == {"1k": 1_364}
    assert row["observed_fixed_output_price_microdollars"] == {"1k": 2_000}
    assert row["routable"] is False
    assert row["routable_reason"] == "fixed-price-change-pending-enclave"


def test_nscale_fixed_price_quarantine_fails_closed_on_corrupt_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "nscale.json"
    manifest.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(nscale, "MANIFEST_PATH", manifest)

    with pytest.raises(RuntimeError, match="existing manifest is unreadable"):
        nscale._quarantine_fixed_price_changes({})


def test_nscale_routable_mixed_manifest_builds_image_and_embedding_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "nscale.json"
    manifest.write_text('{"provider":"nscale","models":[]}', encoding="utf-8")
    chat_prices, manifest_prices, discovered = nscale._discover(_rows())
    apply_canary_results(
        discovered,
        checked_model_ids=set(discovered),
        healthy_model_ids=set(discovered),
    )
    result = nscale.ProviderPricingResult(
        slug=nscale.SLUG,
        prices=manifest_prices,
        source="api",
        fetched_url=nscale.URL,
        price_index_model_ids=frozenset(chat_prices),
    )
    monkeypatch.setattr(nscale, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(nscale, "_DISCOVERED_ROWS", discovered)
    nscale.write_provider_manifest(result)
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    image_models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    embedding_models = catalog_ingest._embedding_models()

    assert nscale.IMAGE_MODEL_ID in image_models
    assert f"{nscale.IMAGE_MODEL_ID}@nscale/prepaid" in endpoints
    embedding = embedding_models[nscale.EMBEDDING_MODEL_ID]
    assert embedding.supports_embeddings is True
    assert embedding.prepaid_available is True
    assert embedding.byok_available is False


def test_nscale_unpriced_image_never_builds_a_catalog_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "nscale.json"
    manifest.write_text('{"provider":"nscale","models":[]}', encoding="utf-8")
    rows = _rows()
    rows[-1]["pricing"] = {"input": 0, "output": "invalid"}
    chat_prices, manifest_prices, discovered = nscale._discover(rows)
    canary_candidates = {
        model_id for model_id, row in discovered.items() if row.get("routable") is not False
    }
    apply_canary_results(
        discovered,
        checked_model_ids=canary_candidates,
        healthy_model_ids=canary_candidates,
    )
    result = nscale.ProviderPricingResult(
        slug=nscale.SLUG,
        prices=manifest_prices,
        source="api",
        fetched_url=nscale.URL,
        price_index_model_ids=frozenset(chat_prices),
    )
    monkeypatch.setattr(nscale, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(nscale, "_DISCOVERED_ROWS", discovered)
    nscale.write_provider_manifest(result)
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    image_models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()

    assert nscale.IMAGE_MODEL_ID not in image_models
    assert f"{nscale.IMAGE_MODEL_ID}@nscale/prepaid" not in endpoints


def test_nscale_mixed_result_indexes_chat_only(monkeypatch) -> None:
    chat_prices, manifest_prices, _rows_by_id = nscale._discover(_rows())
    result = nscale.ProviderPricingResult(
        slug=nscale.SLUG,
        prices=manifest_prices,
        source="api",
        price_index_model_ids=frozenset(chat_prices),
    )
    monkeypatch.setattr(refresh, "_upstream_id_map_for", lambda _slug: {})

    index = refresh._index_provider_prices({nscale.SLUG: result})

    assert set(index) == set(chat_prices)
    assert nscale.EMBEDDING_MODEL_ID not in index
    assert nscale.IMAGE_MODEL_ID not in index


def test_stale_mixed_manifest_recovers_only_chat_token_prices(tmp_path: Path) -> None:
    manifest = tmp_path / "nscale.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "vendor/chat",
                        "model_type": "chat",
                        "routable": True,
                        "input_token_price_per_m": 100_000,
                        "output_token_price_per_m": 400_000,
                    },
                    {
                        "id": "vendor/embedding",
                        "model_type": "embedding",
                        "routable": True,
                        "input_token_price_per_m": 40_000,
                        "output_token_price_per_m": 0,
                    },
                    {
                        "id": "vendor/image",
                        "model_type": "image",
                        "routable": True,
                        "fixed_output_price_microdollars": {"1k": 1_364},
                        "input_token_price_per_m": 0,
                        "output_token_price_per_m": 0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result, error = read_stale_provider_manifest(
        slug="nscale",
        manifest_path=manifest,
        include_in_price_index=True,
    )

    assert error is None
    assert result is not None
    assert result.prices == {"vendor/chat": ModelPrice(100_000, 400_000)}


def test_nscale_catalog_is_fail_closed_and_privacy_is_not_overclaimed() -> None:
    raw = json.loads(nscale.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw["provider"] == nscale.SLUG
    assert raw["model_count"] >= 15
    assert {row["model_type"] for row in raw["models"]} == {
        "chat",
        "embedding",
        "image",
    }
    routable = [row for row in raw["models"] if row.get("routable") is not False]
    blocked = [row for row in raw["models"] if row.get("routable") is False]
    assert routable
    assert blocked
    assert all(
        row.get("routable_reason") == "provider-canary-failed" for row in blocked
    )
    assert all(
        row["input_token_price_per_m"] > 0
        and row["output_token_price_per_m"] > 0
        for row in routable
        if row["model_type"] == "chat"
    )
    assert all(
        row["input_token_price_per_m"] > 0
        and row["output_token_price_per_m"] == 0
        for row in routable
        if row["model_type"] == "embedding"
    )
    assert all(
        row["fixed_output_price_microdollars"] == {"1k": 1_364}
        for row in routable
        if row["model_type"] == "image"
    )

    provider = PROVIDERS[nscale.SLUG]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.supports_embeddings is True
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert nscale.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert nscale.SLUG in refresh.PROVIDER_SLUGS
    assert default_provider_secret_ref(nscale.SLUG) == "env://NSCALE_API_KEY"


def test_nscale_secret_is_runtime_only() -> None:
    root = Path(__file__).parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert (
        'ensure_secret_from_env_file "NSCALE_API_KEY" "trustedrouter-nscale-api-key"'
    ) in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-nscale-api-key"' not in secrets
    assert "NSCALE_API_KEY:trustedrouter-nscale-api-key" not in workflow
