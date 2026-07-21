from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

from pytest import MonkeyPatch

from scripts.pricing import refresh
from scripts.pricing.providers import together
from trusted_router.catalog import endpoints_for_model
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal


class _FakeTogetherModelsResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                {
                    "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    "type": "chat",
                    "pricing": {"input": 1.04, "output": 1.04},
                },
                {
                    "id": "moonshotai/Kimi-K2.7-Code",
                    "type": "chat",
                    "pricing": {"input": 0.95, "output": 4.00, "cached_input": 0.19},
                },
                {
                    "id": "zai-org/GLM-5.2",
                    "type": "chat",
                    "pricing": {"input": 1.40, "output": 4.40},
                },
                {
                    "id": "MiniMaxAI/MiniMax-M3",
                    "type": "chat",
                    "pricing": {"input": 0.30, "output": 1.20},
                },
                {
                    "id": "deepseek-ai/DeepSeek-V4-Pro",
                    "type": "chat",
                    "pricing": {"input": 2.10, "output": 4.40},
                },
                {
                    "id": "deepseek-ai/DeepSeek-R1-0528",
                    "type": "chat",
                    "pricing": {"input": 3.00, "output": 7.00},
                },
                {
                    "id": "Qwen/Qwen3.5-9B",
                    "display_name": "Qwen3.5 9B",
                    "type": "chat",
                    "context_length": 262144,
                    "pricing": {"input": 0.17, "output": 0.25},
                },
            ]
        }


class _FakeTogetherEndpointsResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                {
                    "model": model,
                    "type": "serverless",
                    "state": "STARTED",
                }
                for model in (
                    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    "moonshotai/Kimi-K2.7-Code",
                    "zai-org/GLM-5.2",
                    "MiniMaxAI/MiniMax-M3",
                    "deepseek-ai/DeepSeek-V4-Pro",
                    "Qwen/Qwen3.5-9B",
                )
            ]
        }


class _FakeTogetherClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeTogetherClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def get(
        self, url: str, *, headers: dict[str, str]
    ) -> _FakeTogetherModelsResponse | _FakeTogetherEndpointsResponse:
        assert headers["Authorization"].startswith("Bearer ")
        if url == together.URL:
            return _FakeTogetherModelsResponse()
        assert url == together.SERVERLESS_ENDPOINTS_URL
        return _FakeTogetherEndpointsResponse()


def test_together_llama_33_turbo_price_change_is_mapped(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "fake-together-key")
    monkeypatch.setattr(together.httpx, "Client", _FakeTogetherClient)

    result = together.fetch()

    price = result.prices["meta-llama/llama-3.3-70b-instruct"]
    assert price.prompt_micro_per_m == 1_040_000
    assert price.completion_micro_per_m == 1_040_000


def test_together_api_new_native_id_auto_maps_and_preserves_upstream(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "fake-together-key")
    monkeypatch.setattr(together.httpx, "Client", _FakeTogetherClient)

    result = together.fetch()

    price = result.prices["moonshotai/kimi-k2.7-code"]
    assert price.prompt_micro_per_m == 950_000
    assert price.completion_micro_per_m == 4_000_000
    assert price.tiers[0].prompt_cached_micro_per_m == 190_000
    assert together.UPSTREAM_ID_MAP["moonshotai/kimi-k2.7-code"] == (
        "moonshotai/Kimi-K2.7-Code"
    )

    merged = refresh._merge_snapshot(
        {
            "models": [
                {
                    "id": "moonshotai/kimi-k2.7-code",
                    "name": "Kimi K2.7 Code",
                    "context_length": 262144,
                    "pricing": {"prompt": "0.00000095", "completion": "0.000004"},
                    "endpoints": [],
                }
            ]
        },
        {"moonshotai/kimi-k2.7-code": {"together": price}},
        set(),
    )
    assert merged["models"][0]["endpoints"][0]["model_id"] == (
        "moonshotai/Kimi-K2.7-Code"
    )


def test_together_excludes_deployable_but_non_serverless_models(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "fake-together-key")
    monkeypatch.setattr(together.httpx, "Client", _FakeTogetherClient)

    result = together.fetch()

    assert "deepseek/deepseek-v4-pro" in result.prices
    assert "deepseek/deepseek-r1-0528" not in result.prices


def test_together_refresh_adds_new_started_serverless_chat_models(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "fake-together-key")
    monkeypatch.setattr(together.httpx, "Client", _FakeTogetherClient)
    manifest_path = tmp_path / "together.json"
    manifest_path.write_text(
        json.dumps({"provider": "together", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(together, "MANIFEST_PATH", manifest_path)

    result = together.fetch()
    together.write_provider_manifest(result)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in payload["models"]}
    assert "minimax/minimax-m3" in rows
    assert "qwen/qwen3.5-9b" in rows
    assert rows["qwen/qwen3.5-9b"]["upstream_id"] == "Qwen/Qwen3.5-9B"
    assert rows["qwen/qwen3.5-9b"]["context_length"] == 262_144
    assert "deepseek/deepseek-r1-0528" not in rows


def test_together_money_conversion_never_uses_binary_float_rounding() -> None:
    assert together._row_to_micro_per_m("0.060000000000000005") == 60_000
    assert together._row_to_micro_per_m("0.25999999999999995") == 260_000
    assert together._row_to_micro_per_m("NaN") is None


def test_catalog_exposes_together_llama_33_endpoint_at_new_rate() -> None:
    endpoints = endpoints_for_model("meta-llama/llama-3.3-70b-instruct")
    together_endpoints = [endpoint for endpoint in endpoints if endpoint.provider == "together"]

    # Together's authenticated model feed is authoritative. It may remove a
    # route before or after a deprecation window; stale routes must not be
    # retained merely to satisfy this contract test.
    if not together_endpoints:
        return

    assert {endpoint.usage_type for endpoint in together_endpoints} == {"Credits", "BYOK"}
    assert {endpoint.upstream_id for endpoint in together_endpoints} == {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    }
    prompt_prices = {
        endpoint.prompt_price_microdollars_per_million_tokens
        for endpoint in together_endpoints
    }
    completion_prices = {
        endpoint.completion_price_microdollars_per_million_tokens
        for endpoint in together_endpoints
    }
    assert len(prompt_prices) == 1
    assert len(completion_prices) == 1
    assert next(iter(prompt_prices)) > 0
    assert next(iter(completion_prices)) > 0


def test_model_endpoints_route_uses_provider_specific_together_price(client: Any) -> None:
    response = client.get("/v1/models/meta-llama/llama-3.3-70b-instruct/endpoints")
    assert response.status_code == 200

    together_endpoints = [
        item for item in response.json()["data"] if item["provider"] == "together"
    ]

    if not together_endpoints:
        return

    assert {item["usage_type"] for item in together_endpoints} == {"Credits", "BYOK"}
    assert {item["upstream_id"] for item in together_endpoints} == {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    }
    prompt_prices = {
        item["prompt_price_microdollars_per_million_tokens"]
        for item in together_endpoints
    }
    completion_prices = {
        item["completion_price_microdollars_per_million_tokens"]
        for item in together_endpoints
    }
    assert len(prompt_prices) == 1
    assert len(completion_prices) == 1
    prompt_price = next(iter(prompt_prices))
    completion_price = next(iter(completion_prices))
    assert {item["pricing"]["prompt"] for item in together_endpoints} == {
        microdollars_per_million_tokens_to_token_decimal(prompt_price)
    }
    assert {item["pricing"]["completion"] for item in together_endpoints} == {
        microdollars_per_million_tokens_to_token_decimal(completion_price)
    }


def test_together_pricing_is_on_hourly_refresh_path() -> None:
    workflow = Path(".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")

    assert refresh.PROVIDER_SLUGS[0] == "together"
    assert "0 * * * *" in workflow
    assert "trustedrouter-together-api-key" in workflow
    assert "TOGETHER_API_KEY" in workflow
    assert "deepseek/deepseek-v4-pro" in together.EXPECTED_MODELS
    assert "SERVERLESS_ENDPOINTS_URL" in vars(together)


def test_together_refresh_preserves_native_upstream_model_id() -> None:
    assert (
        together.UPSTREAM_ID_MAP["meta-llama/llama-3.3-70b-instruct"]
        == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    )
    assert (
        refresh._upstream_id_map_for("together")["meta-llama/llama-3.3-70b-instruct"]
        == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    )


def test_hourly_merge_writes_together_native_model_id() -> None:
    merged = refresh._merge_snapshot(
        {
            "models": [
                {
                    "id": "meta-llama/llama-3.3-70b-instruct",
                    "name": "Meta: Llama 3.3 70B Instruct",
                    "context_length": 131072,
                    "pricing": {"prompt": "0.00000088", "completion": "0.00000088"},
                    "endpoints": [
                        {
                            "tr_provider_slug": "together",
                            "model_id": "meta-llama/llama-3.3-70b-instruct",
                            "pricing": {
                                "prompt": "0.00000088",
                                "completion": "0.00000088",
                            },
                        }
                    ],
                }
            ]
        },
        {
            "meta-llama/llama-3.3-70b-instruct": {
                "together": refresh.ModelPrice(1_040_000, 1_040_000)
            }
        },
        set(),
    )

    endpoint = merged["models"][0]["endpoints"][0]
    assert endpoint["model_id"] == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    assert endpoint["pricing"]["prompt"] == "0.00000104"
    assert endpoint["pricing"]["completion"] == "0.00000104"


def test_failed_together_refresh_reuses_committed_snapshot_price() -> None:
    snapshot = {
        "models": [
            {
                "id": "meta-llama/llama-3.3-70b-instruct",
                "endpoints": [
                    {
                        "tr_provider_slug": "together",
                        "pricing": {"prompt": "0.00000104", "completion": "0.00000104"},
                    },
                    {
                        "tr_provider_slug": "parasail",
                        "pricing": {"prompt": "0.00000088", "completion": "0.00000088"},
                    },
                ],
            }
        ]
    }

    stale = refresh._stale_results_from_snapshot(snapshot, ["together"])

    result = stale["together"]
    assert result.source == "stale_snapshot"
    assert result.prices["meta-llama/llama-3.3-70b-instruct"].prompt_micro_per_m == 1_040_000
    assert (
        result.prices["meta-llama/llama-3.3-70b-instruct"].completion_micro_per_m
        == 1_040_000
    )
