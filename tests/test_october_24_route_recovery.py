"""Unavailable routes stay held until a fresh successful provider canary.

Operator and canary holds survive every refresh. A machine-owned feed tombstone
(``delisted-upstream``) is different: it recovers when the provider relists the
model, and a confirmed delisting must be recorded as that tombstone, never as a
credit hold.
"""

import json

import pytest

from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.providers import nextbit, nvidia_nim, pearl, sambanova
from trusted_router.catalog import MODEL_ENDPOINTS


@pytest.mark.parametrize("provider,model", [
    (nextbit, "bsc-lt/alia-40b-instruct-2601"),
    (nvidia_nim, "google/gemma-4-31b-it"),
    (nvidia_nim, "nvidia/nemotron-3-super-120b-a12b"),
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


@pytest.mark.parametrize("model", ["openai/gpt-oss-120b", "meta-llama/llama-3.3-70b-instruct"])
def test_sambanova_account_hold_does_not_disable_working_models(model):
    assert any(e.provider == "sambanova" and e.model_id == model for e in MODEL_ENDPOINTS.values())


def test_sambanova_minimax_m3_delisting_is_a_feed_tombstone_not_a_credit_hold():
    # SambaNova's public /v1/models dropped MiniMax-M3 (MiniMax-M3.1-Flash took its
    # place) and the 2026-10-25 refreshes missed it twice, so the row is a
    # recoverable feed tombstone. The three 402 credit holds are untouched.
    rows = {row["id"]: row for row in json.loads(sambanova.MANIFEST_PATH.read_text())["models"]}
    retired = rows["minimax/minimax-m3"]
    assert retired["upstream_id"] == "MiniMax-M3"
    assert retired["routable"] is False
    assert retired["routable_reason"] == "delisted-upstream"
    assert retired["missing_since"] == "2026-10-25"
    assert not any(
        e.provider == "sambanova" and e.model_id == "minimax/minimax-m3" for e in MODEL_ENDPOINTS.values()
    )
    assert "minimax/minimax-m3" not in sambanova.CATALOG.spec.expected_models
    # Other routes can recover independently on the next authenticated canary.
    # Their live health is not part of MiniMax-M3's tombstone contract.
    assert retired["routable_reason"] != "provider-canary-failed"


def test_nvidia_unavailable_function_is_held_without_disabling_working_route():
    assert not any(
        e.provider == "nvidia-nim" and e.model_id == "nvidia/nemotron-3-super-120b-a12b"
        for e in MODEL_ENDPOINTS.values()
    )
    assert any(
        e.provider == "nvidia-nim" and e.model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
        for e in MODEL_ENDPOINTS.values()
    )


def test_ended_union_preview_is_not_silently_replaced():
    assert not any(e.model_id == "stealth/union-alpha" for e in MODEL_ENDPOINTS.values())


def test_pearl_gemma_relist_does_not_override_confirmed_inference_hold():
    # Discovery relisted the exact ID, but October 28 authenticated inference
    # still returned model_not_found. A listing alone cannot release the hold.
    row = next(
        row for row in json.loads(pearl.MANIFEST_PATH.read_text())["models"]
        if row["id"] == "google/gemma-4-31b-it"
    )
    assert row["upstream_id"] == "google/gemma-4-31B-it"
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-model-not-found"
    assert (row["input_token_price_per_m"], row["output_token_price_per_m"]) == (90_000, 340_000)
    routes = [e for e in MODEL_ENDPOINTS.values() if e.model_id == "google/gemma-4-31b-it"]
    assert not [e for e in routes if e.provider == "pearl"]
    assert {e.provider for e in routes} - {"pearl"}
