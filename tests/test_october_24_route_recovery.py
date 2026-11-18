"""Unavailable routes stay held until a fresh successful provider canary.

Operator and canary holds survive every refresh. A machine-owned feed tombstone
(``delisted-upstream``) is different: it recovers when the provider relists the
model, and a confirmed delisting must be recorded as that tombstone, never as a
credit hold.
"""

import json

import pytest

from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.providers import _direct_openai, nextbit, nvidia_nim, pearl, sambanova
from trusted_router import catalog_ingest
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


# October 24: SambaNova's operator account answered HTTP 402 for three routes
# while its other routes kept serving. Pinned rows, not today's manifest: which
# models SambaNova lists changes hour to hour (Llama 3.3 70B left its feed on
# 2026-11-17), and that is not what this rule is about.
_SAMBANOVA_CREDIT_HELD = {
    "deepseek/deepseek-v3.1": "DeepSeek-V3.1",
    "deepseek/deepseek-v3.2": "DeepSeek-V3.2",
    "google/gemma-4-31b-it": "gemma-4-31B-it",
}
_SAMBANOVA_WORKING = {
    "openai/gpt-oss-120b": "gpt-oss-120b",
    "meta-llama/llama-3.3-70b-instruct": "Meta-Llama-3.3-70B-Instruct",
}
# Production's manifest also carries delisted tombstones (five on 2026-11-18).
# They count toward the mass-prune guard's baseline, so a refresh that darkens
# working routes is published, not discarded by the guard as it would be if the
# working routes were the whole baseline.
_SAMBANOVA_DELISTED = {f"fixture/delisted-{n}": f"Delisted-{n}" for n in range(5)}


def _sambanova_row(model_id, native, **state):
    return {
        "id": model_id, "upstream_id": native, "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_token_price_per_m": 600_000, "output_token_price_per_m": 1_200_000, **state,
    }


@pytest.mark.parametrize("model", list(_SAMBANOVA_WORKING))
def test_sambanova_account_hold_does_not_disable_working_models(model, monkeypatch, tmp_path):
    routes = {**_SAMBANOVA_CREDIT_HELD, **_SAMBANOVA_WORKING}
    manifest = tmp_path / "sambanova.json"
    manifest.write_text(json.dumps({"provider": "sambanova", "models": [
        *(_sambanova_row(m, n, routable=False, routable_reason="provider-canary-failed")
          for m, n in _SAMBANOVA_CREDIT_HELD.items()),
        *(_sambanova_row(m, n, routable=True) for m, n in _SAMBANOVA_WORKING.items()),
        *(_sambanova_row(m, n, routable=False, routable_reason="delisted-upstream",
                         missing_since="2026-10-25") for m, n in _SAMBANOVA_DELISTED.items()),
    ]}))
    # The next refresh lists every route (by the native ids that
    # sambanova.EXPLICIT_MODEL_MAP maps) and re-canaries the held ones; the
    # account still answers 402 for exactly those.
    monkeypatch.setenv("SAMBANOVA_API_KEY", "fixture-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_k: {"data": [
        {"id": native, "pricing": {"prompt": "0.0000006", "completion": "0.0000012"}}
        for native in routes.values()
    ]})
    probed = []

    def probe(**kwargs):
        probed.append(kwargs["model"])
        return kwargs["model"] not in _SAMBANOVA_CREDIT_HELD.values()

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    refresh = _direct_openai.DirectOpenAIProvider(sambanova.CATALOG.spec, manifest_path=manifest)
    notes = refresh.write_provider_manifest(refresh.fetch())
    assert set(_SAMBANOVA_CREDIT_HELD.values()) <= set(probed)
    # The rebuild was published, not discarded by the mass-prune guard.
    assert [note for note in notes if "mass-prune" in note] == []
    assert any(note.startswith("sambanova: refreshed provider_models/sambanova.json") for note in notes)
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert {model_id: (rows[model_id]["routable"], rows[model_id].get("routable_reason"))
            for model_id in routes} == {
        **{model_id: (False, "provider-canary-failed") for model_id in _SAMBANOVA_CREDIT_HELD},
        **{model_id: (True, None) for model_id in _SAMBANOVA_WORKING},
    }

    # The refreshed manifest through the catalog's own ingest and serving filter.
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    served = catalog_ingest._filter_unserved_provider_endpoints(endpoints).values()
    sambanova_routes = {(e.model_id, e.usage_type) for e in served if e.provider == "sambanova"}
    assert (model, "Credits") in sambanova_routes
    assert not {model_id for model_id, _usage in sambanova_routes} & _SAMBANOVA_CREDIT_HELD.keys()


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
