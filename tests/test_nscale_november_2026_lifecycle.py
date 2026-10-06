from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import nscale
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 11, 2, tzinfo=UTC)
_RETIRING = {
    "black-forest-labs/flux.1-schnell": ("black-forest-labs/FLUX.1-schnell", ()),
    "mistralai/devstral-small-2505": ("mistralai/Devstral-Small-2505", ()),
    "moonshotai/kimi-k2.5": ("moonshotai/Kimi-K2.5", ()),
    "nvidia/nemotron-3-nano-30b-a3b-bf16": ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16", ()),
    "qwen/qwen2.5-coder-3b-instruct": ("Qwen/Qwen2.5-Coder-3B-Instruct", ()),
    "qwen/qwen2.5-coder-7b-instruct": ("Qwen/Qwen2.5-Coder-7B-Instruct", ()),
    "qwen/qwen3-235b-a22b": ("Qwen/Qwen3-235B-A22B", ()),
    "openai/gpt-oss-120b": ("openai/gpt-oss-120b", ("z-ai/glm-5.3",)),
    "openai/gpt-oss-20b": ("openai/gpt-oss-20b", ("z-ai/glm-5.3",)),
    "qwen/qwen3-4b-instruct-2507": ("Qwen/Qwen3-4B-Instruct-2507", ("qwen/qwen3.8-27b",)),
    "qwen/qwen3-32b": ("Qwen/Qwen3-32B", ("qwen/qwen3.8-27b",)),
    "qwen/qwen2.5-coder-32b-instruct": ("Qwen/Qwen2.5-Coder-32B-Instruct", ("qwen/qwen3.8-27b",)),
    "qwen/qwen3-14b": ("Qwen/Qwen3-14B", ("qwen/qwen3.8-27b",)),
    "qwen/qwen3-235b-a22b-instruct-2507": (
        "Qwen/Qwen3-235B-A22B-Instruct-2507", ("qwen/qwen3.8-flash-next",),
    ),
}
_RETAINED = {"qwen/qwen3-embedding-8b": "Qwen/Qwen3-Embedding-8B"}


def test_nscale_cutoff_is_the_earliest_named_date_at_midnight_utc() -> None:
    assert provider_lifecycle.NSCALE_NOVEMBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_nscale_retirement_boundary_and_provider_scope(model_id: str) -> None:
    native_id, _ = _RETIRING[model_id]
    retired = provider_lifecycle.provider_model_retired
    assert not retired("nscale", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("nscale", model_id, at=_CUTOFF)
    assert retired("nscale", "different-canonical-id", native_id, at=_CUTOFF)
    assert not retired("unaffected-provider", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_nscale_routed_before_cutover_retired_after_without_substitution(
    monkeypatch: pytest.MonkeyPatch, model_id: str,
) -> None:
    native_id, _ = _RETIRING[model_id]
    endpoints = {}
    for provider in ("nscale", "unaffected-provider"):
        endpoint = ModelEndpoint(
            id=f"{model_id}@{provider}/Credits", model_id=model_id,
            provider=provider, usage_type="Credits", upstream_id=native_id,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model_id)) == 2
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(model_id)
    assert [(e.provider, e.model_id, e.upstream_id) for e in remaining] == [
        ("unaffected-provider", model_id, native_id),
    ]


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_nscale_replacements_are_quoted_not_substituted(model_id: str) -> None:
    _, replacements = _RETIRING[model_id]
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "nscale" and model_id in entry.model_ids
    ]
    assert len(entries) == 1
    assert entries[0].replacement_model_ids == replacements
    assert entries[0].notice_id == f"nscale:{_CUTOFF.isoformat()}"


def test_nscale_embedding_model_survives() -> None:
    for model_id, native_id in _RETAINED.items():
        assert not provider_lifecycle.provider_model_retired(
            "nscale", model_id, native_id, at=_CUTOFF + timedelta(days=1),
        )


def test_nscale_manifest_records_only_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(nscale.MANIFEST_PATH.read_text())["models"]}
    for model_id in _RETAINED:
        assert "retirement_at" not in rows[model_id]
    for model_id, (native_id, replacements) in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-11-02T00:00:00Z"
        assert rows[model_id].get("replacement_model_id") == (replacements[0] if replacements else None)
    annotated = {model_id for model_id, row in rows.items() if "retirement_at" in row}
    assert annotated == set(rows) - set(_RETAINED)
