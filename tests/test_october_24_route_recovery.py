"""Unavailable routes stay held until a fresh successful provider canary."""

import json

import pytest

from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.providers import nextbit, nvidia_nim, sambanova
from trusted_router.catalog import MODEL_ENDPOINTS


@pytest.mark.parametrize("provider,model", [
    (nextbit, "bsc-lt/alia-40b-instruct-2601"),
    (nvidia_nim, "google/gemma-4-31b-it"),
    (sambanova, "deepseek/deepseek-v3.2"),
    (sambanova, "deepseek/deepseek-v3.1"),
    (sambanova, "google/gemma-4-31b-it"),
])
def test_unavailable_route_can_recover_only_after_canary(provider, model, tmp_path):
    row = next(row for row in json.loads(provider.MANIFEST_PATH.read_text())["models"] if row["id"] == model)
    if row.get("routable") is False:
        assert not any(e.provider == provider.SLUG and e.model_id == model for e in MODEL_ENDPOINTS.values())
    # A successful future refresh is allowed to revive the production row.
    row.update(routable=False, routable_reason="provider-canary-failed")
    manifest = tmp_path / "provider.json"
    manifest.write_text(json.dumps({"models": [row]}))
    assert models_requiring_canary(manifest, {model}) == {model}
    for healthy in (False, True):
        candidate = {model: dict(row)}
        apply_canary_results(candidate, checked_model_ids={model}, healthy_model_ids={model} if healthy else set())
        assert candidate[model]["routable"] is healthy


@pytest.mark.parametrize("model", [
    "deepseek/deepseek-v3.1", "deepseek/deepseek-v3.1-terminus",
    "kwaipilot/kat-coder-pro-v2.5", "openai/gpt-4.1-mini",
    "openai/gpt-5.2-codex", "openai/gpt-5.3-codex", "openai/gpt-5.4",
    "openai/gpt-5.4-mini", "openai/gpt-5.4-nano", "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3-vl-235b-a22b-thinking", "z-ai/glm-4.6", "z-ai/glm-4.7",
    "deepseek/deepseek-v4-flash-vision-exp",
])
def test_retired_atlas_routes_are_not_advertised(model):
    assert not any(e.provider == "atlas-cloud" and e.model_id == model for e in MODEL_ENDPOINTS.values())


@pytest.mark.parametrize("model", ["openai/gpt-4.1-mini", "openai/gpt-5.4-mini"])
def test_atlas_retirement_does_not_remove_openai_direct(model):
    assert any(e.provider == "openai" and e.model_id == model for e in MODEL_ENDPOINTS.values())


@pytest.mark.parametrize("model", [
    "openai/gpt-oss-120b", "meta-llama/llama-3.3-70b-instruct", "minimax/minimax-m3",
])
def test_sambanova_account_hold_does_not_disable_working_models(model):
    assert any(e.provider == "sambanova" and e.model_id == model for e in MODEL_ENDPOINTS.values())


def test_ended_union_preview_is_not_silently_replaced():
    assert not any(e.model_id == "stealth/union-alpha" for e in MODEL_ENDPOINTS.values())


def test_pearl_gemma_retirement_preserves_other_providers():
    routes = [e for e in MODEL_ENDPOINTS.values() if e.model_id == "google/gemma-4-31b-it"]
    assert routes
    assert not any(e.provider == "pearl" for e in routes)
