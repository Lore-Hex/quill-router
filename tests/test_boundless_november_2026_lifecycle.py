from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import boundless
from trusted_router import provider_lifecycle
from trusted_router.catalog_registry import MODEL_ENDPOINTS

_CUTOFF = datetime(2026, 11, 26, 23, 59, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-11-26T23:59:00Z"
_V41_FLASH_1017 = "deepseek/deepseek-v4.1-flash-1017"
# The eight models Boundless's November 24 notice names, keyed by TrustedRouter
# model id: (Boundless short name in the notice, upstream_id, quoted replacement).
_RETIRING = {
    "xiaomi/mimo-v2.6-pro": ("mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro", ()),
    "moonshotai/kimi-k3": ("kimi-k3", "moonshotai/kimi-k3", ("moonshotai/kimi-k3.1-pro",)),
    "qwen/qwen3.6-35b-a3b": ("qwen3.6", "qwen/qwen3.6-35b-a3b", (_V41_FLASH_1017,)),
    "z-ai/glm-5.2": ("glm-5.2", "z-ai/glm-5.2", ()),
    "z-ai/glm-5.3": ("glm-5.3", "z-ai/glm-5.3", ()),
    "deepseek/deepseek-v4-flash-0731": ("dsv4", "deepseek/deepseek-v4-flash-0731", (_V41_FLASH_1017,)),
    "minimax/minimax-m3": ("minimax-m3", "minimax/minimax-m3", ()),
    "deepseek/deepseek-v4.1-flash": ("deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash", (_V41_FLASH_1017,)),
}
# Boundless routes the notice does not name. Ten more models retire but are
# unlisted, so these stay routable until Boundless publishes the full list.
_NOT_NAMED = {
    "deepseek/deepseek-v4.1-flash-1017",
    "google/gemma-4-31b-it",
    "minimax/minimax-m3.1-flash",
    "moonshotai/kimi-k3.1-pro",
    "nvidia/nemotron-3-super-120b-a12b",
    "xiaomi/mimo-v2.6-flash",
    "z-ai/glm-5.3-flash",
    "openai/gpt-oss-120b",
}


def _manifest_rows() -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(boundless.MANIFEST_PATH.read_text())["models"]}


def test_boundless_cutoff_is_the_announced_utc_instant() -> None:
    assert provider_lifecycle.BOUNDLESS_NOV_26_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_named_routes_serve_before_the_cutoff_and_retire_at_it(model_id: str) -> None:
    _, native_id, _ = _RETIRING[model_id]
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=before)
    assert provider_lifecycle.provider_model_retired("boundless", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("boundless", model_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired(
        "boundless", "unrelated/model", native_id, at=_CUTOFF,
    )


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_named_routes_quote_only_like_for_like_replacements(model_id: str) -> None:
    _, _, replacements = _RETIRING[model_id]
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "boundless" and model_id in entry.model_ids
    ]
    assert len(entries) == 1
    assert entries[0].effective_at == _CUTOFF
    assert entries[0].replacement_model_ids == replacements
    assert entries[0].notice_id == f"boundless:{_CUTOFF.isoformat()}"


def test_quoted_replacements_are_catalog_models_that_stay_on_boundless() -> None:
    catalog_models = {endpoint.model_id for endpoint in MODEL_ENDPOINTS.values()}
    quoted = {replacement for _, _, replacements in _RETIRING.values() for replacement in replacements}
    assert quoted == {"moonshotai/kimi-k3.1-pro", _V41_FLASH_1017}
    for replacement in quoted:
        assert replacement in catalog_models
        assert not provider_lifecycle.provider_model_retired(
            "boundless", replacement, replacement, at=_CUTOFF + timedelta(days=1),
        )


def test_unnamed_boundless_routes_are_not_retired() -> None:
    later = _CUTOFF + timedelta(days=1)
    for model_id in _NOT_NAMED:
        assert not provider_lifecycle.provider_model_retired("boundless", model_id, model_id, at=later)


def test_other_providers_keep_the_retiring_models() -> None:
    later = _CUTOFF + timedelta(days=1)
    for model_id, (_, native_id, _) in _RETIRING.items():
        assert not provider_lifecycle.provider_model_retired("deepinfra", model_id, native_id, at=later)
        others = {
            endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.model_id == model_id and endpoint.provider != "boundless"
        }
        assert others, model_id


def test_manifest_maps_notice_names_and_records_only_the_named_cutover() -> None:
    rows = _manifest_rows()
    for model_id, (short_name, native_id, replacements) in _RETIRING.items():
        row = rows[model_id]
        assert row["title"] == short_name
        assert row["upstream_id"] == native_id
        assert row["retirement_at"] == _MANIFEST_CUTOFF
        assert row.get("replacement_model_id") == (replacements[0] if replacements else None)
    annotated = {model_id for model_id, row in rows.items() if row.get("retirement_at") == _MANIFEST_CUTOFF}
    assert annotated == set(_RETIRING)
