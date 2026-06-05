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
