from __future__ import annotations

from scripts.pricing.providers import crusoe


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_crusoe_fetch_discovers_prices_and_native_ids(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai/GLM-5.2",
                "pricing": {
                    "prompt": "1.40",
                    "completion": "4.40",
                    "input_cache_reads": "0.26",
                },
            },
            {
                "id": "deepseek-ai/Deepseek-V4-Flash",
                "pricing": {
                    "prompt": "0.14",
                    "completion": "0.28",
                    "input_cache_reads": "0.03",
                },
            },
            {
                "id": "moonshotai/Kimi-K2.6",
                "pricing": {
                    "prompt": "0.70",
                    "completion": "3.50",
                    "input_cache_reads": "0.35",
                },
            },
            {
                "id": "openai/gpt-oss-120b",
                "pricing": {
                    "prompt": "0.05",
                    "completion": "0.25",
                    "input_cache_reads": "0.05",
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

    monkeypatch.setattr(crusoe.httpx, "Client", FakeClient)

    result = crusoe.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    flash = result.prices["deepseek/deepseek-v4-flash"]

    assert glm.prompt_micro_per_m == 1_400_000
    assert glm.completion_micro_per_m == 4_400_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 260_000
    assert flash.prompt_micro_per_m == 140_000
    assert crusoe.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai/GLM-5.2"
    assert (
        crusoe.UPSTREAM_ID_MAP["deepseek/deepseek-v4-flash"]
        == "deepseek-ai/Deepseek-V4-Flash"
    )
