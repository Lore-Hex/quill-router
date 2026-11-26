"""Unavailable routes stay held until a fresh successful provider canary.

Operator and canary holds survive every refresh. A machine-owned feed tombstone
(``delisted-upstream``) is different: it recovers when the provider relists the
model, and a confirmed delisting must be recorded as that tombstone, never as a
credit hold.
"""

import json
from datetime import timedelta

import pytest

from scripts.pricing.base import ProviderPricingResult
from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.providers import _direct_openai, pearl, sambanova
from trusted_router import catalog_ingest, provider_lifecycle
from trusted_router.provider_contracts import (
    PEARL_GEMMA_4_31B_HOLD_REASON,
    PEARL_GEMMA_4_31B_MODEL_ID,
)

# Every test here states the provider rows it needs. Which routes a provider
# lists, which canaries pass and which tombstones exist change with each hourly
# refresh, so these rules are checked on fixture manifests fed through the
# catalog's own ingest and serving filter, never on today's catalog.
_HELD = {"routable": False, "routable_reason": "provider-canary-failed"}
_DELISTED = {"routable": False, "routable_reason": "delisted-upstream", "missing_since": "2026-10-24"}


def _row(model_id, native=None, **state):
    return {
        "id": model_id, "upstream_id": native or model_id, "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_token_price_per_m": 600_000, "output_token_price_per_m": 1_200_000, **state,
    }


def _served(monkeypatch, tmp_path, manifests=None, *, usage_type="Credits"):
    """The (provider, model) routes the catalog serves from tmp_path's
    manifests, after writing any given here: prepaid only, or every usage type
    when usage_type is None."""
    for slug, rows in (manifests or {}).items():
        (tmp_path / f"{slug}.json").write_text(json.dumps({"provider": slug, "models": rows}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    return {
        (e.provider, e.model_id)
        for e in catalog_ingest._filter_unserved_provider_endpoints(endpoints).values()
        if usage_type is None or e.usage_type == usage_type
    }


@pytest.mark.parametrize("slug,model,native", [
    ("nextbit", "bsc-lt/alia-40b-instruct-2601", "alia-salamandra:40b"),
    ("nvidia-nim", "google/gemma-4-31b-it", "google/gemma-4-31b-it"),
    ("nvidia-nim", "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-super-120b-a12b"),
    ("sambanova", "deepseek/deepseek-v3.2", "DeepSeek-V3.2"),
    ("sambanova", "deepseek/deepseek-v3.1", "DeepSeek-V3.1"),
    ("sambanova", "google/gemma-4-31b-it", "gemma-4-31B-it"),
])
def test_unavailable_route_can_recover_only_after_canary(slug, model, native, monkeypatch, tmp_path):
    held = _row(model, native, **_HELD)
    assert (slug, model) not in _served(monkeypatch, tmp_path, {slug: [held]})
    assert models_requiring_canary(tmp_path / f"{slug}.json", {model}) == {model}
    for healthy in (False, True):
        candidate = {model: dict(held)}
        apply_canary_results(candidate, checked_model_ids={model}, healthy_model_ids={model} if healthy else set())
        assert candidate[model]["routable"] is healthy
        assert ((slug, model) in _served(monkeypatch, tmp_path, {slug: [candidate[model]]})) is healthy


# October 24: Atlas Cloud's feed stopped listing these routes and the refresh
# tombstoned them. A tombstone recovers if Atlas relists the model, so what is
# checked is that a tombstoned Atlas row is not advertised, not that Atlas
# never lists these models again.
_ATLAS_RETIRED = (
    "deepseek/deepseek-v3.1", "deepseek/deepseek-v3.1-terminus",
    "kwaipilot/kat-coder-pro-v2.5", "openai/gpt-4.1-mini",
    "openai/gpt-5.2-codex", "openai/gpt-5.3-codex", "openai/gpt-5.4",
    "openai/gpt-5.4-mini", "openai/gpt-5.4-nano", "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3-vl-235b-a22b-thinking", "z-ai/glm-4.6", "z-ai/glm-4.7",
    "deepseek/deepseek-v4-flash-vision-exp",
)
_ATLAS_LIVE = _row("z-ai/glm-5.2", "zai-org/glm-5.2")


@pytest.mark.parametrize("model", _ATLAS_RETIRED)
def test_retired_atlas_routes_are_not_advertised(model, monkeypatch, tmp_path):
    served = _served(monkeypatch, tmp_path, {"atlas-cloud": [_row(model, **_DELISTED), _ATLAS_LIVE]})
    assert ("atlas-cloud", model) not in served
    # Control: the fixture manifest is ingested, and its listed route is served.
    assert ("atlas-cloud", _ATLAS_LIVE["id"]) in served


@pytest.mark.parametrize("model", ["openai/gpt-4.1-mini", "openai/gpt-5.4-mini"])
def test_atlas_retirement_does_not_remove_openai_direct(model, monkeypatch, tmp_path):
    served = _served(monkeypatch, tmp_path, {
        "atlas-cloud": [_row(model, **_DELISTED), _ATLAS_LIVE],
        "openai": [_row(model, model.removeprefix("openai/"), routable=True)],
    })
    assert ("openai", model) in served
    assert ("atlas-cloud", model) not in served


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


def _before_the_sambanova_llama_retirement(monkeypatch):
    # The delisting became a reviewed retirement on 2026-11-26 05:00 UTC
    # (provider_lifecycle). These rules are about October 24, when the route
    # was working, so they are checked just before that cutover.
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: provider_lifecycle.NOVEMBER_26_DELISTED_RETIREMENT_AT - timedelta(seconds=1),
    )


@pytest.mark.parametrize("model", list(_SAMBANOVA_WORKING))
def test_sambanova_account_hold_does_not_disable_working_models(model, monkeypatch, tmp_path):
    _before_the_sambanova_llama_retirement(monkeypatch)
    routes = {**_SAMBANOVA_CREDIT_HELD, **_SAMBANOVA_WORKING}
    manifest = tmp_path / "sambanova.json"
    manifest.write_text(json.dumps({"provider": "sambanova", "models": [
        *(_row(m, n, **_HELD) for m, n in _SAMBANOVA_CREDIT_HELD.items()),
        *(_row(m, n, routable=True) for m, n in _SAMBANOVA_WORKING.items()),
        *(_row(m, n, **_DELISTED) for m, n in _SAMBANOVA_DELISTED.items()),
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
    served = _served(monkeypatch, tmp_path)
    assert ("sambanova", model) in served
    assert not {("sambanova", m) for m in _SAMBANOVA_CREDIT_HELD} & served


def test_sambanova_minimax_m3_delisting_is_a_feed_tombstone_not_a_credit_hold(monkeypatch, tmp_path):
    # SambaNova's public /v1/models dropped MiniMax-M3 (MiniMax-M3.1-Flash took its
    # place) and the 2026-10-25 refreshes missed it twice. Two refreshes whose
    # feed omits it must leave a recoverable feed tombstone, never a credit
    # hold, and leave the 402 credit holds and the working routes as they were.
    _before_the_sambanova_llama_retirement(monkeypatch)
    retired, retired_native = "minimax/minimax-m3", "MiniMax-M3"
    listed = {**_SAMBANOVA_CREDIT_HELD, **_SAMBANOVA_WORKING}
    manifest = tmp_path / "sambanova.json"
    manifest.write_text(json.dumps({"provider": "sambanova", "models": [
        *(_row(m, n, **_HELD) for m, n in _SAMBANOVA_CREDIT_HELD.items()),
        *(_row(m, n, routable=True) for m, n in _SAMBANOVA_WORKING.items()),
        _row(retired, retired_native, routable=True),
        *(_row(m, n, **_DELISTED) for m, n in _SAMBANOVA_DELISTED.items()),
    ]}))
    monkeypatch.setenv("SAMBANOVA_API_KEY", "fixture-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_k: {"data": [
        {"id": native, "pricing": {"prompt": "0.0000006", "completion": "0.0000012"}}
        for native in listed.values()
    ]})
    monkeypatch.setattr(
        _direct_openai, "probe_openai_chat",
        lambda **kwargs: kwargs["model"] not in _SAMBANOVA_CREDIT_HELD.values(),
    )

    def refresh():
        adapter = _direct_openai.DirectOpenAIProvider(sambanova.CATALOG.spec, manifest_path=manifest)
        notes = adapter.write_provider_manifest(adapter.fetch())
        assert not [note for note in notes if "mass-prune" in note]
        rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
        # Every refresh, not just the last, leaves the listed routes as they were.
        assert {m: (rows[m]["routable"], rows[m].get("routable_reason")) for m in listed} == {
            **{m: (False, "provider-canary-failed") for m in _SAMBANOVA_CREDIT_HELD},
            **{m: (True, None) for m in _SAMBANOVA_WORKING},
        }
        return rows

    first = refresh()[retired]
    assert first["routable"] is True and first["missing_since"]
    rows = refresh()
    assert rows[retired]["upstream_id"] == retired_native
    assert (rows[retired]["routable"], rows[retired]["routable_reason"]) == (False, "delisted-upstream")
    assert rows[retired]["missing_since"] == first["missing_since"]
    served = _served(monkeypatch, tmp_path)
    assert ("sambanova", retired) not in served
    assert {("sambanova", m) for m in _SAMBANOVA_WORKING} <= served
    assert retired not in sambanova.CATALOG.spec.expected_models


def test_nvidia_unavailable_function_is_held_without_disabling_working_route(monkeypatch, tmp_path):
    held, working = "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3.5-lightning-30b-a3b"
    served = _served(monkeypatch, tmp_path, {"nvidia-nim": [_row(held, **_HELD), _row(working, routable=True)]})
    assert ("nvidia-nim", held) not in served
    assert ("nvidia-nim", working) in served


def test_pearl_gemma_relist_does_not_override_confirmed_inference_hold(monkeypatch, tmp_path):
    # Discovery relisted the exact ID, but October 28 authenticated inference
    # still returned model_not_found. A listing alone cannot release the hold.
    model, native = PEARL_GEMMA_4_31B_MODEL_ID, "google/gemma-4-31B-it"
    manifest = tmp_path / "pearl.json"
    manifest.write_text(json.dumps({"provider": "pearl", "models": [
        _row(model, native, routable=False, routable_reason=PEARL_GEMMA_4_31B_HOLD_REASON),
    ]}))
    monkeypatch.setattr(pearl, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(pearl, "UPSTREAM_ID_MAP", {})
    prices, relisted = pearl._parse_catalog({"data": [
        {"id": native, "pricing": {"prompt": "0.00000009", "completion": "0.00000034"}},
    ]})
    relisted[model]["routable"] = True
    monkeypatch.setattr(pearl, "_DISCOVERED_MANIFEST_ROWS", relisted)
    pearl.write_provider_manifest(ProviderPricingResult(
        slug="pearl", prices=prices, source="api", fetched_url=pearl.URL,
    ))
    row = json.loads(manifest.read_text())["models"][0]
    assert (row["routable"], row["routable_reason"]) == (False, PEARL_GEMMA_4_31B_HOLD_REASON)
    # Even a manifest that calls the relisted route routable does not serve it
    # on Pearl, and the hold does not take the model from its other hosts.
    served = _served(monkeypatch, tmp_path, {
        "pearl": [_row(model, native, routable=True)],
        "deepinfra": [_row(model, native, routable=True)],
    })
    assert ("pearl", model) not in served
    assert ("deepinfra", model) in served
