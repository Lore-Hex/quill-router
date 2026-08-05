from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.providers import scaleway
from trusted_router.catalog import (
    EU_FOCUSED_PROVIDER_ORDER,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
)
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS


def _money(value: str) -> dict[str, object]:
    decimal = Decimal(value)
    units = int(decimal)
    nanos = int((decimal - units) * Decimal(1_000_000_000))
    return {"currencyCode": "EUR", "units": units, "nanos": nanos}


def _pricing_row(
    native_id: str,
    *,
    provider: str,
    api: str = "/v1/chat/completions",
    input_price: str = "1.80",
    output_price: str = "5.50",
    context: int = 256_000,
    tasks: list[str] | None = None,
    tools: bool = True,
    reasoning: bool = True,
) -> dict[str, Any]:
    region: dict[str, Any] = {
        "region": "fr-par",
        "inputTokenPrice": {
            "perMillionTokens": {
                "isApproximation": False,
                "value": _money(input_price),
            }
        },
    }
    if api == "/v1/chat/completions":
        region["outputTokenPrice"] = {
            "perMillionTokens": {
                "isApproximation": False,
                "value": _money(output_price),
            }
        }
    return {
        "apiId": native_id,
        "name": native_id,
        "providerName": provider,
        "contextWindow": context,
        "maxOutputTokens": 16_384,
        "regions": [region],
        "supportedApis": [api],
        "tasks": tasks or ["chat"],
        "toolCallingSupported": tools,
        "reasoning": reasoning,
    }


def _pricing_html(*rows: dict[str, Any]) -> str:
    return (
        "<html><script>"
        + 'window.__DATA__={"generativeApis":{"models":'
        + json.dumps(list(rows), separators=(",", ":"))
        + "}};</script></html>"
    )


def _live_payload(*model_ids: str) -> dict[str, Any]:
    return {"object": "list", "data": [{"id": model_id} for model_id in model_ids]}


def test_scaleway_uses_structured_prices_and_exact_decimal_fx() -> None:
    prices, discovered, skipped = scaleway._catalog(
        _live_payload("glm-5.2", "qwen3-embedding-8b", "whisper-large-v3"),
        _pricing_html(
            _pricing_row("glm-5.2", provider="Zai"),
            _pricing_row(
                "qwen3-embedding-8b",
                provider="Qwen",
                api="/v1/embeddings",
                input_price="0.10",
                context=0,
                tasks=["embeddings"],
                tools=False,
                reasoning=False,
            ),
            _pricing_row(
                "whisper-large-v3",
                provider="OpenAI",
                api="/v1/audio/transcriptions",
                tools=False,
                reasoning=False,
            ),
        ),
        Decimal("1.1554"),
    )

    assert prices["z-ai/glm-5.2"].prompt_micro_per_m == 2_079_720
    assert prices["z-ai/glm-5.2"].completion_micro_per_m == 6_354_700
    assert prices["qwen/qwen3-embedding-8b"].prompt_micro_per_m == 115_540
    assert prices["qwen/qwen3-embedding-8b"].completion_micro_per_m == 0
    assert discovered["qwen/qwen3-embedding-8b"]["context_length"] == 32_768
    assert "openai/whisper-large-v3" not in discovered
    assert skipped == 1


def test_scaleway_rounds_converted_cost_up_to_integer_microdollar() -> None:
    assert scaleway._usd_micro_per_m(Decimal("0.000001"), Decimal("1.000001")) == 2


def test_scaleway_parses_official_ecb_rate() -> None:
    xml = """<Envelope><Cube><Cube time="2026-08-05">
      <Cube currency="USD" rate="1.1554" />
    </Cube></Cube></Envelope>"""

    assert scaleway._parse_eur_usd(xml) == Decimal("1.1554")


def test_scaleway_fails_closed_when_live_model_has_no_price() -> None:
    with pytest.raises(RuntimeError, match="live models missing structured prices"):
        scaleway._catalog(
            _live_payload("glm-5.2", "future-model"),
            _pricing_html(_pricing_row("glm-5.2", provider="Zai")),
            Decimal("1.15"),
        )


def test_scaleway_auto_discovers_future_known_publisher_model() -> None:
    future = _pricing_row(
        "qwen4-future",
        provider="Qwen",
        input_price="0.25",
        output_price="1.50",
        tasks=["chat", "vision"],
    )

    prices, discovered, _skipped = scaleway._catalog(
        _live_payload("qwen4-future"),
        _pricing_html(future),
        Decimal("1.2"),
    )

    assert "qwen/qwen4-future" in prices
    assert discovered["qwen/qwen4-future"]["upstream_id"] == "qwen4-future"
    assert discovered["qwen/qwen4-future"]["input_modalities"] == ["text", "image"]


def test_scaleway_manifest_contains_live_chat_and_embedding_catalog() -> None:
    manifest = json.loads(scaleway.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in manifest["models"]}

    assert manifest["provider"] == "scaleway"
    assert manifest["price_scale"] == "microdollars_per_million"
    assert len(rows) == 14
    assert rows["z-ai/glm-5.2"]["upstream_id"] == "glm-5.2"
    assert rows["z-ai/glm-5.2"]["provider_regions"] == ["fr-par"]
    assert rows["qwen/qwen3-embedding-8b"]["model_type"] == "embedding"
    assert rows["baai/bge-multilingual-gemma2"]["endpoints"] == ["embeddings"]
    assert all(row["input_token_price_per_m"] > 0 for row in rows.values())


def test_scaleway_catalog_routes_are_prepaid_only_and_eu_focused() -> None:
    provider = PROVIDERS["scaleway"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.supports_embeddings is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert "scaleway" in EU_FOCUSED_PROVIDER_ORDER
    assert EU_FOCUSED_PROVIDER_ORDER.index("scaleway") == (
        EU_FOCUSED_PROVIDER_ORDER.index("mistral") + 1
    )

    endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "scaleway"
    ]
    assert endpoints
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    assert any(
        endpoint.model_id == "z-ai/glm-5.2" and endpoint.upstream_id == "glm-5.2"
        for endpoint in endpoints
    )
    assert MODELS["qwen/qwen3-embedding-8b"].supports_embeddings is True


def test_scaleway_public_provider_and_model_endpoint_shapes(client: Any) -> None:
    providers = {row["id"]: row for row in client.get("/v1/providers").json()["data"]}
    provider = providers["scaleway"]
    assert provider["name"] == "Scaleway"
    assert provider["supports_prepaid"] is True
    assert provider["supports_byok"] is False
    assert provider["provider_zero_data_retention"] is False

    response = client.get("/v1/models/z-ai/glm-5.2/endpoints")
    assert response.status_code == 200
    route = next(
        row for row in response.json()["data"] if row["provider_name"] == "Scaleway"
    )
    assert route["upstream_id"] == "glm-5.2"


def test_scaleway_hourly_refresh_and_secret_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names)
        for slug, url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    assert discoverable["scaleway"] == (
        "https://api.scaleway.ai/v1/models",
        ("SCALEWAY_SECRET_KEY",),
    )
    assert OPENAI_COMPATIBLE_PROVIDERS["scaleway"] == (
        ("SCALEWAY_SECRET_KEY",),
        "https://api.scaleway.ai/v1",
    )

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    rollout = (root / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'ensure_secret_from_env_file "SCALEWAY_SECRET_KEY" '
        '"trustedrouter-scaleway-api-key"'
    ) in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-scaleway-api-key"' in secrets
    assert (
        'add_secret_env_if_exists "SCALEWAY_SECRET_KEY" '
        '"trustedrouter-scaleway-api-key"'
    ) in rollout
    assert "SCALEWAY_SECRET_KEY:trustedrouter-scaleway-api-key" in workflow
