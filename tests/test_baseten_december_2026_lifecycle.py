from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.pricing.providers import baseten
from tests.provider_holds import row_on_unserved_route_hold
from trusted_router import provider_lifecycle
from trusted_router.catalog_registry import MODEL_ENDPOINTS

_CUTOFF = datetime(2026, 12, 24, 1, 0, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-12-24T01:00:00Z"
# Baseten's November 25 notice, by display name -> (model id, Baseten native id).
_RETIRING = {
    "DeepSeek V4.1 Flash": ("deepseek/deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash"),
    "DeepSeek V4.1 Flash Fast": (
        "deepseek/deepseek-v4.1-flash-fast", "deepseek-ai/DeepSeek-V4.1-Flash-Fast",
    ),
    "DeepSeek V4 Flash 0731": (
        "deepseek/deepseek-v4-flash-0731", "deepseek-ai/DeepSeek-V4-Flash-0731",
    ),
    "GLM 5.2 Fast": ("z-ai/glm-5.2-fast", "zai-org/GLM-5.2-Fast"),
    "Nemotron 3 Ultra": (
        "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B",
    ),
}
# Neighbouring Baseten Model APIs the notice does not name. These must keep serving.
_RETAINED = {
    "deepseek/deepseek-v4.1-flash-1017": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "deepseek/deepseek-v4.1-flash-1017-fast": "deepseek-ai/DeepSeek-V4.1-Flash-1017-Fast",
    "deepseek/deepseek-v4-pro-0813": "deepseek-ai/DeepSeek-V4-Pro-0813",
    "z-ai/glm-5.2": "zai-org/GLM-5.2",
    "z-ai/glm-5.3": "zai-org/GLM-5.3",
    "z-ai/glm-5.3-fast": "zai-org/GLM-5.3-Fast",
    "z-ai/glm-5.3-flash": "zai-org/GLM-5.3-Flash",
    "nvidia/nemotron-3.5-super-vl-120b-a12b": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B",
}
# Baseten's changelog replacements, quoted only where like-for-like or newer.
# Its Nemotron 3.5 Super VL suggestion for Nemotron 3 Ultra is a smaller tier.
_REPLACEMENTS = {
    "deepseek/deepseek-v4.1-flash": ("deepseek/deepseek-v4.1-flash-1017",),
    "deepseek/deepseek-v4-flash-0731": ("deepseek/deepseek-v4.1-flash-1017",),
    "deepseek/deepseek-v4.1-flash-fast": ("deepseek/deepseek-v4.1-flash-1017-fast",),
    "z-ai/glm-5.2-fast": ("z-ai/glm-5.3-fast",),
}
# Baseten was the only live host for these, so they leave the catalog at the cutoff.
_BASETEN_ONLY = {"deepseek/deepseek-v4.1-flash-fast", "z-ai/glm-5.2-fast"}


def _manifest_rows() -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}


def _december_entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "baseten" and entry.effective_at == _CUTOFF
    ]


def test_baseten_december_cutoff_is_5pm_pacific_standard_time() -> None:
    announced = datetime(2026, 12, 23, 17, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert announced.utcoffset() == timedelta(hours=-8)  # PST: DST ended 2026-11-01
    assert announced.astimezone(UTC) == _CUTOFF
    assert provider_lifecycle.BASETEN_DECEMBER_2026_RETIREMENT_AT == _CUTOFF


def test_entries_retire_exactly_the_five_notice_models() -> None:
    entries = _december_entries()
    assert set().union(*(entry.model_ids for entry in entries)) == {
        model_id for model_id, _ in _RETIRING.values()
    }
    assert set().union(*(entry.upstream_ids for entry in entries)) == {
        native_id for _, native_id in _RETIRING.values()
    }
    for entry in entries:
        assert entry.notice_id == f"baseten:{_CUTOFF.isoformat()}"


@pytest.mark.parametrize("display_name", sorted(_RETIRING))
def test_only_like_for_like_replacements_are_quoted(display_name: str) -> None:
    model_id, _ = _RETIRING[display_name]
    (entry,) = [e for e in _december_entries() if model_id in e.model_ids]
    assert entry.replacement_model_ids == _REPLACEMENTS.get(model_id, ())


def test_quoted_replacements_stay_on_baseten() -> None:
    later = _CUTOFF + timedelta(days=1)
    rows = _manifest_rows()
    for replacement in {r for rs in _REPLACEMENTS.values() for r in rs}:
        assert replacement in _RETAINED
        # Served, unless the refresh itself holds the row as unserved.
        assert f"{replacement}@baseten/prepaid" in MODEL_ENDPOINTS or row_on_unserved_route_hold(
            rows.get(replacement, {})
        ), replacement
        assert not provider_lifecycle.provider_model_retired(
            "baseten", replacement, _RETAINED[replacement], at=later,
        )


@pytest.mark.parametrize("display_name", sorted(_RETIRING))
def test_notice_names_are_baseten_manifest_rows(display_name: str) -> None:
    model_id, native_id = _RETIRING[display_name]
    row = _manifest_rows()[model_id]
    assert row["display_name"] == display_name
    assert row["upstream_id"] == native_id
    assert row["retirement_at"] == _MANIFEST_CUTOFF


@pytest.mark.parametrize("display_name", sorted(_RETIRING))
def test_retiring_routes_serve_before_the_cutoff_and_retire_at_it(display_name: str) -> None:
    model_id, native_id = _RETIRING[display_name]
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("baseten", model_id, native_id, at=before)
    assert not provider_lifecycle.provider_model_retired("baseten", model_id, at=before)
    assert provider_lifecycle.provider_model_retired("baseten", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("baseten", model_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("baseten", "unrelated/model", native_id, at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), sorted(_RETAINED.items()))
def test_unnamed_baseten_models_keep_serving(model_id: str, native_id: str) -> None:
    later = _CUTOFF + timedelta(days=1)
    assert not provider_lifecycle.provider_model_retired("baseten", model_id, native_id, at=later)
    assert "retirement_at" not in _manifest_rows()[model_id]


@pytest.mark.parametrize("display_name", sorted(_RETIRING))
def test_other_providers_routes_are_untouched(display_name: str) -> None:
    model_id, _ = _RETIRING[display_name]
    later = _CUTOFF + timedelta(days=1)
    others = {
        endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == model_id and endpoint.provider != "baseten"
        and not provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
        )
    }
    # The catalog may already be built past the cutoff; only the Baseten-only
    # models are allowed to have no other live host.
    assert bool(others) is (model_id not in _BASETEN_ONLY), (model_id, others)


def test_nemotron_3_ultra_keeps_its_liberty_hosts() -> None:
    # Liberty 4.x pins Ultra to Fireworks and NVIDIA NIM, never Baseten.
    later = _CUTOFF + timedelta(days=1)
    for endpoint in MODEL_ENDPOINTS.values():
        if endpoint.model_id == "nvidia/nemotron-3-ultra-550b-a55b" and endpoint.provider in {
            "fireworks", "nvidia-nim",
        }:
            assert not provider_lifecycle.provider_model_retired(
                endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
            )


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_stale_feed_cannot_rediscover_retired_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    retiring = dict(_RETIRING.values())
    payload = {"data": [
        {"id": native, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
        for native in (retiring | _RETAINED).values()
    ]}
    monkeypatch.setattr(
        baseten.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(baseten, "UPSTREAM_ID_MAP", dict(baseten.UPSTREAM_ID_MAP))
    monkeypatch.setattr(baseten, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(baseten, "MANIFEST_PATH", tmp_path / "baseten.json")
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = baseten.fetch()
    expected = set(_RETAINED) if after_cutoff else set(retiring | _RETAINED)
    assert set(result.prices) == expected
    assert set(baseten._DISCOVERED_MANIFEST_ROWS) == expected
