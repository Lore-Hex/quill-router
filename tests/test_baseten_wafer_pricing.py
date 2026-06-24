from __future__ import annotations

from scripts.pricing.providers import baseten, wafer


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_baseten_fetch_discovers_prices_without_float_drift(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai-org/GLM-5.2",
                "pricing": {
                    "prompt": "0.0000014",
                    "completion": "0.0000044",
                    "input_cache_read": "0.00000026",
                },
            },
            {
                "id": "moonshotai/Kimi-K2.7-Code",
                "pricing": {
                    "prompt": "0.00000095",
                    "completion": "0.000004",
                    "input_cache_read": "0.00000016",
                },
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(payload)

    monkeypatch.setattr(baseten.httpx, "Client", FakeClient)

    result = baseten.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    kimi = result.prices["moonshotai/kimi-k2.7-code"]

    assert glm.prompt_micro_per_m == 1_400_000
    assert glm.completion_micro_per_m == 4_400_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 260_000
    assert kimi.prompt_micro_per_m == 950_000
    assert kimi.completion_micro_per_m == 4_000_000
    assert baseten.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2"


def test_wafer_fetch_discovers_prices_and_native_ids(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "GLM-5.2",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 120,
                        "output_cents_per_million": 410,
                        "cache_read_cents_per_million": 20,
                    }
                },
            },
            {
                "id": "Kimi-K2.7-Code",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 95,
                        "output_cents_per_million": 400,
                        "cache_read_cents_per_million": 19,
                    }
                },
            },
            {
                "id": "MiniMax-M3",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 33,
                        "output_cents_per_million": 132,
                        "cache_read_cents_per_million": 7,
                    }
                },
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(payload)

    monkeypatch.setattr(wafer.httpx, "Client", FakeClient)

    result = wafer.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    kimi = result.prices["moonshotai/kimi-k2.7-code"]
    minimax = result.prices["minimax/minimax-m3"]

    assert glm.prompt_micro_per_m == 1_200_000
    assert glm.completion_micro_per_m == 4_100_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 200_000
    assert kimi.prompt_micro_per_m == 950_000
    assert minimax.completion_micro_per_m == 1_320_000
    assert wafer.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "GLM-5.2"
    assert wafer.UPSTREAM_ID_MAP["moonshotai/kimi-k2.7-code"] == "Kimi-K2.7-Code"
