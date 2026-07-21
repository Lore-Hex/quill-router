from __future__ import annotations

from scripts.pricing.providers import friendli


def test_friendli_fetch_discovers_glm_52(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai-org/GLM-5.2",
                "pricing": {
                    "input": "0.0000014",
                    "output": "0.0000044",
                    "input_cache_read": "0.00000026",
                },
            },
            {
                "id": "zai-org/GLM-5",
                "pricing": {
                    "input": "0.0000010",
                    "output": "0.0000032",
                },
            },
            {
                "id": "meta-llama-3.3-70b-instruct",
                "pricing": {
                    "input": "0.0000006",
                    "output": "0.0000006",
                },
            },
        ]
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse()

    monkeypatch.setattr(friendli.httpx, "Client", FakeClient)

    result = friendli.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    llama = result.prices["meta-llama/llama-3.3-70b-instruct"]

    assert glm.prompt_micro_per_m == 1_400_000
    assert glm.completion_micro_per_m == 4_400_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 260_000
    assert "z-ai/glm-5" not in result.prices
    assert llama.prompt_micro_per_m == 600_000
    assert friendli.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2"
    assert (
        friendli.UPSTREAM_ID_MAP["meta-llama/llama-3.3-70b-instruct"]
        == "meta-llama-3.3-70b-instruct"
    )


def test_friendli_money_conversion_uses_exact_per_token_units() -> None:
    assert friendli._price_to_micro_per_m("0.0000014") == 1_400_000
    assert friendli._price_to_micro_per_m("0.00000006") == 60_000
    assert friendli._price_to_micro_per_m("NaN") is None
