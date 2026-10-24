"""Unavailable routes stay held until a fresh successful provider canary."""

import json

import pytest

from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.providers import nextbit, nvidia_nim
from trusted_router.catalog import MODEL_ENDPOINTS


@pytest.mark.parametrize("provider,model", [
    (nextbit, "bsc-lt/alia-40b-instruct-2601"),
    (nvidia_nim, "google/gemma-4-31b-it"),
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


def test_atlas_retirement_does_not_remove_openai_direct():
    assert not any(e.provider == "atlas-cloud" and e.model_id == "openai/gpt-4.1-mini" for e in MODEL_ENDPOINTS.values())
    assert any(e.provider == "openai" and e.model_id == "openai/gpt-4.1-mini" for e in MODEL_ENDPOINTS.values())


def test_ended_union_preview_is_not_silently_replaced():
    assert not any(e.model_id == "stealth/union-alpha" for e in MODEL_ENDPOINTS.values())
