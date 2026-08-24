from __future__ import annotations

from scripts.pricing.providers import deepinfra


def test_deepinfra_llama_31_70b_deprecation_aliases_to_canonical_model() -> None:
    canonical = "meta-llama/llama-3.1-70b-instruct"

    assert (
        deepinfra._NATIVE_TO_OR_ID["meta-llama/Meta-Llama-3.1-70B-Instruct"]
        == canonical
    )
    assert (
        deepinfra._NATIVE_TO_OR_ID["meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"]
        == canonical
    )


def test_deepinfra_fetch_discovers_glm_52(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai-org/GLM-5.2",
                "metadata": {
                    "pricing": {
                        "input_tokens": 1.2,
                        "output_tokens": 4.2,
                        "cache_read_tokens": 0.2,
                    }
                },
            }
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

    monkeypatch.setattr(deepinfra.httpx, "Client", FakeClient)

    result = deepinfra.fetch()
    price = result.prices["z-ai/glm-5.2"]

    assert price.prompt_micro_per_m == 1_200_000
    assert price.completion_micro_per_m == 4_200_000
    assert deepinfra.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2"
