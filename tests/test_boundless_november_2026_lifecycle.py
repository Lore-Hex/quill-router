from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import boundless
from tests.provider_holds import row_taken_down_by_refresh
from trusted_router import catalog_ingest, provider_lifecycle
from trusted_router.catalog_registry import MODEL_ENDPOINTS

_CUTOFF = datetime(2026, 11, 26, 23, 59, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-11-26T23:59:00Z"
_V41_FLASH_1017 = "deepseek/deepseek-v4.1-flash-1017"
# All 18 models Boundless's November 24 notices retire, keyed by TrustedRouter
# model id: (Boundless name in the notice, manifest row title, upstream_id,
# quoted replacement). The first notice named the first eight; the follow-up
# lists the rest.
_RETIRING = {
    "xiaomi/mimo-v2.6-pro": ("mimo-v2.6-pro", "mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro", ()),
    "moonshotai/kimi-k3": ("kimi-k3", "kimi-k3", "moonshotai/kimi-k3", ("moonshotai/kimi-k3.1-pro",)),
    "qwen/qwen3.6-35b-a3b": ("qwen3.6", "qwen3.6", "qwen/qwen3.6-35b-a3b", (_V41_FLASH_1017,)),
    "z-ai/glm-5.2": ("glm-5.2", "glm-5.2", "z-ai/glm-5.2", ()),
    "z-ai/glm-5.3": ("glm-5.3", "glm-5.3", "z-ai/glm-5.3", ()),
    "deepseek/deepseek-v4-flash-0731": (
        "dsv4", "dsv4", "deepseek/deepseek-v4-flash-0731", (_V41_FLASH_1017,),
    ),
    "minimax/minimax-m3": ("minimax-m3", "minimax-m3", "minimax/minimax-m3", ()),
    "deepseek/deepseek-v4.1-flash": (
        "deepseek-v4.1-flash", "deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash", (_V41_FLASH_1017,),
    ),
    # Follow-up notice: the remaining open models (no replacement quoted).
    "deepseek/deepseek-v4-pro-0813": (
        "deepseek-v4-pro-0813", "deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro-0813", (),
    ),
    "nvidia/nemotron-3-super-120b-a12b": (
        "nemotron3-super", "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-super-120b-a12b", (),
    ),
    "google/gemma-4-31b-it": ("gemma4-31b", "google/gemma-4-31b-it", "google/gemma-4-31b-it", ()),
    "minimax/minimax-m3.1-flash": (
        "minimax/minimax-m3.1-flash", "minimax/minimax-m3.1-flash", "minimax/minimax-m3.1-flash", (),
    ),
    "openai/gpt-oss-120b": ("openai/gpt-oss-120b", "openai/gpt-oss-120b", "openai/gpt-oss-120b", ()),
    # Follow-up notice: frontier models (never routed through Boundless).
    "anthropic/claude-fable-5.1": (
        "claude-fable-5.1", "claude-fable-5.1", "anthropic/claude-fable-5.1", (),
    ),
    "anthropic/claude-opus-5": ("claude-opus-5", "claude-opus-5", "anthropic/claude-opus-5", ()),
    "openai/gpt-5.6-luna": ("gpt-5.6-luna", "gpt-5.6-luna", "openai/gpt-5.6-luna", ()),
    "openai/gpt-5.6-sol": ("gpt-5.6-sol", "gpt-5.6-sol", "openai/gpt-5.6-sol", ()),
    "openai/gpt-6-sol": ("gpt-6-sol", "gpt-6-sol", "openai/gpt-6-sol", ()),
}
_FRONTIER = {
    "anthropic/claude-fable-5.1",
    "anthropic/claude-opus-5",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.6-sol",
    "openai/gpt-6-sol",
}
# The delisted legacy manifest row whose upstream id is the notice name
# nemotron3-super; it retires with the 120B card it was renamed to.
_LEGACY_NEMOTRON = ("nvidia/nemotron-3-super", "nemotron3-super")
# Boundless's open-model lineup after the cutoff. These must keep serving.
_OPEN_SURVIVORS = {
    _V41_FLASH_1017,
    "z-ai/glm-5.3-flash",
    "moonshotai/kimi-k3.1-pro",
    "xiaomi/mimo-v2.6-flash",
}
# Boundless's frontier lineup after the cutoff: not retired (still maker-only
# in TrustedRouter's catalog, so never routed through Boundless).
_FRONTIER_SURVIVORS = {
    "anthropic/claude-fable-5.2",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-haiku-5.5",
    "openai/gpt-6.2-astra",
    "openai/gpt-6.2-sol",
    "openai/gpt-6.1-terra",
    "openai/gpt-6-luna",
}


def _manifest_rows() -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(boundless.MANIFEST_PATH.read_text())["models"]}


def _boundless_entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "boundless" and entry.effective_at == _CUTOFF
    ]


def test_boundless_cutoff_is_the_announced_utc_instant() -> None:
    assert provider_lifecycle.BOUNDLESS_NOV_26_RETIREMENT_AT == _CUTOFF


def test_all_eighteen_notice_models_are_retired_at_the_one_cutoff() -> None:
    assert len(_RETIRING) == 18
    retired = set().union(*(entry.model_ids for entry in _boundless_entries()))
    assert retired == set(_RETIRING)
    # The legacy row retires by upstream id only, so it never becomes a model.
    assert _LEGACY_NEMOTRON[0] not in retired


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_notice_names_map_to_the_retired_route(model_id: str) -> None:
    notice_name, _, _, _ = _RETIRING[model_id]
    assert boundless.canonical_model_id(notice_name) == model_id


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_retiring_routes_serve_before_the_cutoff_and_retire_at_it(model_id: str) -> None:
    notice_name, _, native_id, _ = _RETIRING[model_id]
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=before)
    assert provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("boundless", model_id, at=_CUTOFF)
    for upstream in {native_id, notice_name}:
        assert not provider_lifecycle.provider_model_retired("boundless", "unrelated/model", upstream, at=before)
        assert provider_lifecycle.provider_model_retired("boundless", "unrelated/model", upstream, at=_CUTOFF)


def test_legacy_short_upstream_ids_retire_too() -> None:
    legacy_id, legacy_upstream = _LEGACY_NEMOTRON
    for model_id, native_id in (_LEGACY_NEMOTRON, ("unrelated/model", "gemma4-31b")):
        assert not provider_lifecycle.provider_model_retired(
            "boundless", model_id, native_id, at=_CUTOFF - timedelta(minutes=1),
        )
        assert provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("boundless", "unrelated/model", legacy_upstream, at=_CUTOFF)
    assert not provider_lifecycle.provider_model_retired("boundless", legacy_id, at=_CUTOFF)


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_retiring_routes_quote_only_like_for_like_replacements(model_id: str) -> None:
    _, _, _, replacements = _RETIRING[model_id]
    entries = [entry for entry in _boundless_entries() if model_id in entry.model_ids]
    assert len(entries) == 1
    assert entries[0].replacement_model_ids == replacements
    assert entries[0].notice_id == f"boundless:{_CUTOFF.isoformat()}"


def test_quoted_replacements_are_catalog_models_that_stay_on_boundless() -> None:
    catalog_models = {endpoint.model_id for endpoint in MODEL_ENDPOINTS.values()}
    quoted = {replacement for *_, replacements in _RETIRING.values() for replacement in replacements}
    assert quoted == {"moonshotai/kimi-k3.1-pro", _V41_FLASH_1017}
    assert quoted <= _OPEN_SURVIVORS
    for replacement in quoted:
        assert replacement in catalog_models


def test_boundless_survivors_keep_serving_after_the_cutoff() -> None:
    later = _CUTOFF + timedelta(days=1)
    rows = _manifest_rows()
    for model_id in _OPEN_SURVIVORS | _FRONTIER_SURVIVORS:
        native_id = rows[model_id]["upstream_id"]
        assert not provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=later)
        assert "retirement_at" not in rows[model_id]
    # A survivor the refresh itself holds as unserved is dark until that hold
    # lifts. It is still not retired, which the loop above checks for all.
    held = {model_id for model_id in _OPEN_SURVIVORS if row_taken_down_by_refresh(rows[model_id])}
    for model_id in _OPEN_SURVIVORS - held:
        assert rows[model_id].get("routable") is not False
        assert f"{model_id}@boundless/prepaid" in MODEL_ENDPOINTS
    assert _OPEN_SURVIVORS - held <= catalog_ingest._authoritative_provider_model_ids("boundless", at=later)


def test_frontier_rows_are_not_routed_through_boundless() -> None:
    boundless_models = {e.model_id for e in MODEL_ENDPOINTS.values() if e.provider == "boundless"}
    assert not boundless_models & (_FRONTIER | _FRONTIER_SURVIVORS)


def test_refresh_cannot_readmit_retired_rows_after_the_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh Boundless listing that still prices every row: after the cutoff
    # only the survivors reach the price index or the authoritative route set.
    later = _CUTOFF + timedelta(minutes=1)
    rows = _manifest_rows()
    listed = set(_RETIRING) | _OPEN_SURVIVORS
    result = ProviderPricingResult(
        slug=boundless.SLUG,
        prices={model_id: ModelPrice(100_000, 200_000) for model_id in listed},
        source="api",
    )
    monkeypatch.setattr(refresh, "_upstream_id_map_for", lambda _slug: {
        model_id: rows[model_id]["upstream_id"] for model_id in listed
    })
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(minutes=1))
    assert set(refresh._index_provider_prices({boundless.SLUG: result})) == listed
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: later)
    assert set(refresh._index_provider_prices({boundless.SLUG: result})) == _OPEN_SURVIVORS

    admitted = catalog_ingest._authoritative_provider_model_ids("boundless", at=later)
    assert not admitted & (set(_RETIRING) | {_LEGACY_NEMOTRON[0]})


def test_every_retiring_model_stays_on_another_provider() -> None:
    later = _CUTOFF + timedelta(days=1)
    for model_id, (_, _, native_id, _) in _RETIRING.items():
        assert not provider_lifecycle.provider_model_retired("deepinfra", model_id, native_id, at=later)
        others = {
            endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.model_id == model_id and endpoint.provider != "boundless"
            and not provider_lifecycle.provider_model_retired(
                endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
            )
        }
        assert others, model_id


def test_gpt_oss_120b_stays_held_until_the_cutoff() -> None:
    # The provider already rejects it, so the operator hold keeps it dark
    # until the retirement takes over; Boundless will not serve it again.
    assert boundless._OPERATOR_HOLD_REASONS == {"openai/gpt-oss-120b": "provider-access-unavailable"}
    assert "openai/gpt-oss-120b@boundless/prepaid" not in MODEL_ENDPOINTS


def test_manifest_maps_notice_names_and_records_the_cutover() -> None:
    rows = _manifest_rows()
    for model_id, (notice_name, title, native_id, replacements) in _RETIRING.items():
        row = rows[model_id]
        assert row["title"] == title
        if row["upstream_id"] != native_id:
            # The post-cutover listing can keep just the unpriced notice alias.
            # No other substitution, or active route under that alias, qualifies.
            assert row["upstream_id"] == notice_name
            assert row["routable"] is False
            assert row["routable_reason"] in {"awaiting-price", "delisted-upstream"}
        assert row["retirement_at"] == _MANIFEST_CUTOFF
        assert row.get("replacement_model_id") == (replacements[0] if replacements else None)
    legacy_id, legacy_upstream = _LEGACY_NEMOTRON
    assert rows[legacy_id]["title"] == rows[legacy_id]["upstream_id"] == legacy_upstream
    annotated = {model_id for model_id, row in rows.items() if row.get("retirement_at") == _MANIFEST_CUTOFF}
    assert annotated == set(_RETIRING) | {legacy_id}
