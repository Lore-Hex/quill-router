from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from pytest import MonkeyPatch

from scripts.pricing import refresh
from scripts.pricing.providers import together
from trusted_router.catalog import MODELS, endpoints_for_model


class _FakeTogetherResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                {
                    "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    "pricing": {"input": 1.04, "output": 1.04},
                },
                {
                    "id": "moonshotai/Kimi-K2.7-Code",
                    "pricing": {"input": 0.95, "output": 4.00},
                }
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

    def get(self, url: str, *, headers: dict[str, str]) -> _FakeTogetherResponse:
        assert url == together.URL
        assert headers["Authorization"].startswith("Bearer ")
        return _FakeTogetherResponse()


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


def _a_together_served_model() -> str:
    """Pick a model the live catalog currently routes to Together.

    Together's served set churns across snapshot refreshes (it dropped
    meta-llama/llama-3.3-70b-instruct in the 2026-06 catalog reconcile), so
    these tests assert the Together *wiring* against whatever it serves NOW
    rather than pinning a specific model + exact price that breaks on every
    refresh. The invariant under test is structural: a Together endpoint
    surfaces dual Credits/BYOK usage with a positive provider-direct price.
    """
    for model_id in sorted(MODELS):
        if any(ep.provider == "together" for ep in endpoints_for_model(model_id)):
            return model_id
    raise AssertionError("catalog has no Together-served model")


def test_catalog_together_endpoint_uses_provider_direct_pricing() -> None:
    model_id = _a_together_served_model()
    together_endpoints = [
        endpoint
        for endpoint in endpoints_for_model(model_id)
        if endpoint.provider == "together"
    ]
    assert together_endpoints
    assert {endpoint.usage_type for endpoint in together_endpoints} == {"Credits", "BYOK"}
    # One upstream id shared by the Credits + BYOK endpoints for the model.
    assert len({endpoint.upstream_id for endpoint in together_endpoints}) == 1
    # Provider-direct prices are positive — never OR's $0 cross-check value.
    for endpoint in together_endpoints:
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0


def test_model_endpoints_route_exposes_together_provider_price(client: Any) -> None:
    model_id = _a_together_served_model()
    response = client.get(f"/v1/models/{model_id}/endpoints")
    assert response.status_code == 200

    together_endpoints = [
        item for item in response.json()["data"] if item["provider"] == "together"
    ]
    assert together_endpoints
    assert {item["usage_type"] for item in together_endpoints} == {"Credits", "BYOK"}
    assert len({item["upstream_id"] for item in together_endpoints}) == 1
    for item in together_endpoints:
        assert item["prompt_price_microdollars_per_million_tokens"] > 0
        assert item["completion_price_microdollars_per_million_tokens"] > 0
        assert float(item["pricing"]["prompt"]) > 0
        assert float(item["pricing"]["completion"]) > 0


def test_together_pricing_is_on_hourly_refresh_path() -> None:
    workflow = Path(".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")

    assert refresh.PROVIDER_SLUGS[0] == "together"
    assert "0 * * * *" in workflow
    assert "trustedrouter-together-api-key" in workflow
    assert "TOGETHER_API_KEY" in workflow
    assert "meta-llama/llama-3.3-70b-instruct" in together.EXPECTED_MODELS


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
