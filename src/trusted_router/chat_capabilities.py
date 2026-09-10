"""Verified request controls, not merely the ability to return reasoning text."""

from __future__ import annotations


def reasoning_modes(provider: str, model_id: str) -> list[str]:
    # Only direct hybrid-model routes whose native thinking switch is mapped
    # by the attested gateway. Do not infer this from supports_reasoning:
    # forced-thinking models and third-party hosts have different contracts.
    if provider == "zai" and model_id in {
        "z-ai/glm-4.5", "z-ai/glm-4.5-air", "z-ai/glm-4.6",
        "z-ai/glm-4.7", "z-ai/glm-5", "z-ai/glm-5.1", "z-ai/glm-5.2",
    }:
        return ["off", "on"]
    if provider == "deepseek" and (
        model_id.startswith("deepseek/deepseek-v4-")
        or model_id in {"deepseek/deepseek-v3.1", "deepseek/deepseek-v3.2"}
    ):
        return ["off", "on"]
    if provider == "kimi" and model_id in {
        "moonshotai/kimi-k2.5", "moonshotai/kimi-k2.6", "moonshotai/kimi-k2.7",
    }:
        return ["off", "on"]
    return []
