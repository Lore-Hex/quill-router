"""The September presets are new graphs, never rewrites of published versions."""

import json
from pathlib import Path

import pytest

from trusted_router.catalog import (
    MODELS,
    endpoints_for_model,
    meta_candidate_models,
    model_to_openrouter_shape,
)


@pytest.mark.parametrize(
    "alias,version,primitive,components",
    [
        ("prometheus", "4.0", "synth", [
            "xiaomi/mimo-v2.6-pro", "z-ai/glm-5.3", "moonshotai/kimi-k3",
            "deepseek/deepseek-v4.1-flash", "minimax/minimax-m3", "qwen/qwen3.8-2.4t-a95b",
        ]),
        ("zeus", "3.0", "synth", [
            "openai/gpt-6-astra", "anthropic/claude-fable-5.1", "google/gemini-3.8-flash",
            "xiaomi/mimo-v2.6-pro", "z-ai/glm-5.3", "moonshotai/kimi-k3",
            "deepseek/deepseek-v4.1-flash",
        ]),
        ("plato", "4.0", "advisor", [
            "xiaomi/mimo-v2.6-pro", "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3",
            "trustedrouter/prometheus-4.0",
        ]),
        ("socrates", "3.0", "advisor", [
            "xiaomi/mimo-v2.6-pro-ultraspeed", "xiaomi/mimo-v2.6-pro",
            "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3", "trustedrouter/zeus-3.0",
        ]),
    ],
)
def test_new_combos_are_cataloged(alias: str, version: str, primitive: str, components: list[str]) -> None:
    canonical = f"trustedrouter/{alias}-{version}"
    for model_id in (canonical, f"trustedrouter/{alias}"):
        shape = model_to_openrouter_shape(MODELS[model_id])
        assert shape["context_length"] == 1_000_000
        assert shape["trustedrouter"]["canonical_model_id"] == canonical
        assert shape["trustedrouter"]["orchestration_primitive"] == primitive
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["byok_available"] is False
        assert [model.id for model in meta_candidate_models(model_id)] == components


@pytest.mark.parametrize(
    "model_id,providers",
    [
        ("qwen/qwen3.8-2.4t-a95b", ("novita", "together")),
        ("minimax/minimax-m3", ("minimax", "novita")),
    ],
)
def test_retained_panel_members_have_two_million_token_routes(model_id: str, providers: tuple[str, ...]) -> None:
    root = Path(__file__).parents[1] / "src/trusted_router/data/provider_models"
    assert len(set(providers)) >= 2
    for provider in providers:
        registration = json.loads((root / f"{provider}.json").read_text())
        model = next(item for item in registration["models"] if item["id"] == model_id)
        assert model.get("routable") is not False
        assert model["context_length"] >= 1_000_000
        assert any(
            ep.provider == provider and ep.usage_type == "Credits"
            for ep in endpoints_for_model(model_id)
        )
