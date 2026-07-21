from __future__ import annotations

from scripts.pricing.model_ids import (
    canonicalize_native_model_id,
    canonicalize_unqualified_model_id,
)
from scripts.pricing.parsers import kimi
from scripts.pricing.providers import tinfoil


def test_canonicalize_provider_native_ids_for_new_model_discovery() -> None:
    assert canonicalize_native_model_id("moonshotai/Kimi-K2.7-Code") == "moonshotai/kimi-k2.7-code"
    assert canonicalize_native_model_id("Qwen/Qwen3.5-397B-A17B") == ("qwen/qwen3.5-397b-a17b")
    assert canonicalize_native_model_id("zai-org/GLM-5.2") == "z-ai/glm-5.2"
    assert canonicalize_native_model_id("MiniMaxAI/MiniMax-M2.7") == ("minimax/minimax-m2.7")


def test_canonicalize_unqualified_aggregator_model_families() -> None:
    assert canonicalize_unqualified_model_id("gpt-oss-120b") == ("openai/gpt-oss-120b")
    assert canonicalize_unqualified_model_id("zai-glm-5p3") == "z-ai/glm-5.3"
    assert (
        canonicalize_unqualified_model_id("accounts/fireworks/models/kimi-k2p8-code")
        == "moonshotai/kimi-k2.8-code"
    )
    assert canonicalize_unqualified_model_id("phala/qwen3.7-max") == ("qwen/qwen3.7-max")
    assert canonicalize_unqualified_model_id("unrelated-model") is None


def test_kimi_parser_accepts_new_kimi_family_ids_without_hand_map() -> None:
    text = (
        '["kimi-k2.7-code", "1M tokens", <>{"$"}0.19</>, '
        '<>{"$"}0.95</>, <>{"$"}4.00</>, "262,144 tokens"]'
    )

    parsed = kimi.parse(text)

    assert parsed["moonshotai/kimi-k2.7-code"] == {
        "prompt_micro_per_m": 950_000,
        "completion_micro_per_m": 4_000_000,
        "prompt_cached_micro_per_m": 190_000,
    }


def test_tinfoil_native_ids_track_june_2026_replacements() -> None:
    assert tinfoil._NATIVE_TO_OR_ID["kimi-k2-7-code"] == "moonshotai/kimi-k2.7-code"
    assert tinfoil._NATIVE_TO_OR_ID["glm-5-2"] == "z-ai/glm-5.2"
    assert tinfoil._NATIVE_TO_OR_ID["gemma4-31b"] == "google/gemma-4-31b-it"
    assert tinfoil._NATIVE_TO_OR_ID["qwen3-vl-30b"] == "qwen/qwen3-vl-30b-a3b-instruct"
    assert tinfoil.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "glm-5-2"
    assert tinfoil.UPSTREAM_ID_MAP["google/gemma-4-31b-it"] == "gemma4-31b"
