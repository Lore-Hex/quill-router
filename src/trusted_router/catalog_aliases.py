"""Reviewed model equivalences, independent of provider-native request IDs."""

from collections.abc import Mapping
from dataclasses import replace

from trusted_router.catalog_data import Model, ModelEndpoint

MODEL_LISTING_ALIASES = {
    "qwen/qwen-3-8-27b": "qwen/qwen3.8-27b",
    "lightning-ai/qwen3.8-27b": "qwen/qwen3.8-27b",
    "cerebras/qwen-3.8-27b": "qwen/qwen3.8-27b",
    # Nebius publishes its native Hugging Face spellings, which duplicated
    # models already listed under their catalog ids.
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "Qwen/Qwen3-Embedding-8B": "qwen/qwen3-embedding-8b",
    "Qwen/Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "zai-org/glm-5.1": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "MiniMaxAI/MiniMax-M3": "minimax/minimax-m3",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
}


def canonical_listing_id(model_id: str, models: Mapping[str, object]) -> str:
    target = MODEL_LISTING_ALIASES.get(model_id, model_id)
    return target if target in models else model_id


def is_listing_alias(model_id: str, models: Mapping[str, object]) -> bool:
    return canonical_listing_id(model_id, models) != model_id


def consolidate_model_routes(
    models: dict[str, Model], endpoints: dict[str, ModelEndpoint],
) -> None:
    """Add reviewed aliases' routes to the canonical model without changing aliases.

    Run after admission holds and lifecycle filtering. Old model/endpoint IDs
    remain valid for existing callers and outstanding billing authorizations.
    Canonical endpoints win collisions; never infer equivalence from spelling.
    """
    for alias, target in MODEL_LISTING_ALIASES.items():
        if alias not in models or target not in models:
            continue
        for endpoint in tuple(endpoints.values()):
            if endpoint.model_id != alias:
                continue
            suffix = "byok" if endpoint.is_byok else "prepaid"
            identity = f"{target}@{endpoint.provider}/{suffix}"
            endpoints.setdefault(identity, replace(endpoint, id=identity, model_id=target))
