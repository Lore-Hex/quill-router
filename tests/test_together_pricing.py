from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from pytest import MonkeyPatch

from scripts.pricing import refresh
from scripts.pricing.providers import together
from trusted_router.catalog import endpoints_for_model


class _FakeTogetherResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                {
                    "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    "pricing": {"input": 1.04, "output": 1.04},
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


def test_catalog_exposes_together_llama_33_endpoint_at_new_rate() -> None:
    endpoints = endpoints_for_model("meta-llama/llama-3.3-70b-instruct")
    together_endpoints = [endpoint for endpoint in endpoints if endpoint.provider == "together"]

    assert {endpoint.usage_type for endpoint in together_endpoints} == {"Credits", "BYOK"}
    assert {endpoint.upstream_id for endpoint in together_endpoints} == {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    }
    assert {
        endpoint.prompt_price_microdollars_per_million_tokens
        for endpoint in together_endpoints
    } == {1_144_000}
    assert {
        endpoint.completion_price_microdollars_per_million_tokens
        for endpoint in together_endpoints
    } == {1_144_000}


def test_together_pricing_is_on_hourly_refresh_path() -> None:
    workflow = Path(".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")

    assert refresh.PROVIDER_SLUGS[0] == "together"
    assert "0 * * * *" in workflow
    assert "trustedrouter-together-api-key" in workflow
    assert "TOGETHER_API_KEY" in workflow
    assert "meta-llama/llama-3.3-70b-instruct" in together.EXPECTED_MODELS


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
