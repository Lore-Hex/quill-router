from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import fireworks
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 25, tzinfo=UTC)
_RETIRING = {
    "deepseek/deepseek-v4-flash-0731": "accounts/fireworks/models/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-pro-0813": "accounts/fireworks/models/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-flash-vision-exp": "accounts/fireworks/models/deepseek-v4-flash-vision-exp",
    "z-ai/glm-5.2": "accounts/fireworks/models/glm-5p2",
    "z-ai/glm-5.2-fast": "accounts/fireworks/routers/glm-5p2-fast",
    "meta-models/muse-glimmer-30b": "accounts/fireworks/models/muse-glimmer-30b",
    "moonshotai/kimi-k2.6": "accounts/fireworks/models/kimi-k2p6",
    "moonshotai/kimi-k2.7-code": "accounts/fireworks/models/kimi-k2p7-code",
}
_RETAINED = {
    "deepseek/deepseek-v4p1-flash": "accounts/fireworks/models/deepseek-v4p1-flash",
    "z-ai/glm-5.3": "accounts/fireworks/models/glm-5p3",
    "z-ai/glm-5.3-fast": "accounts/fireworks/routers/glm-5p3-fast",
    "z-ai/glm-5.3-flash": "accounts/fireworks/models/glm-5p3-flash",
    "moonshotai/kimi-k3": "accounts/fireworks/models/kimi-k3",
    "moonshotai/kimi-k3-fast": "accounts/fireworks/routers/kimi-k3-fast",
    "nvidia/nemotron-3.5-lightning": "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
    "minimax/minimax-m3": "accounts/fireworks/models/minimax-m3",
    "openai/gpt-oss-120b": "accounts/fireworks/models/gpt-oss-120b",
}


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_fireworks_september_exact_cutoff_and_provider_scope(model_id: str, native_id: str) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("fireworks", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("fireworks", model_id, at=_CUTOFF)
    assert retired("fireworks", "different-canonical-id", native_id, at=_CUTOFF)
    assert retired("fireworks", model_id, native_id, at=_CUTOFF + timedelta(days=1))
    assert not retired("another-provider", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_fireworks_september_runtime_cutoff_without_reload(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str,
) -> None:
    endpoints = {}
    for provider in ("fireworks", "another-provider"):
        for usage in ("Credits", "BYOK"):
            for variant in ("standard", "us"):
                endpoint = ModelEndpoint(
                    id=f"{model_id}@{provider}/{usage}/{variant}", model_id=model_id,
                    provider=provider, usage_type=usage, upstream_id=native_id,
                )
                endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model_id)) == 8
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(model_id)
    assert len(remaining) == 4
    assert {endpoint.provider for endpoint in remaining} == {"another-provider"}
    assert {endpoint.model_id for endpoint in remaining} == {model_id}


@pytest.mark.parametrize(("model_id", "native_id"), _RETAINED.items())
def test_fireworks_september_preserves_other_routes(model_id: str, native_id: str) -> None:
    assert not provider_lifecycle.provider_model_retired("fireworks", model_id, native_id, at=_CUTOFF)


def test_fireworks_september_stale_prices_cannot_restore_retired_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="fireworks", source="api", fetched_url=fireworks.MODELS_URL,
        prices={model: ModelPrice(1_000_000, 2_000_000) for model in _RETIRING | _RETAINED},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert set(refresh._index_provider_prices({"fireworks": result})) == set(_RETIRING | _RETAINED)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"fireworks": result})) == set(_RETAINED)


def test_fireworks_september_manifest_marks_only_announced_routes() -> None:
    rows = {row["id"]: row for row in json.loads(fireworks.MANIFEST_PATH.read_text())["models"]}
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-09-25T00:00:00Z"
    for model_id in _RETAINED:
        assert not rows[model_id].get("retirement_at")


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_fireworks_september_discovery_rejects_stale_native_feed(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool,
) -> None:
    rows = _RETIRING | _RETAINED
    monkeypatch.setattr(fireworks, "UPSTREAM_ID_MAP", dict(fireworks.UPSTREAM_ID_MAP))
    monkeypatch.setattr(fireworks, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(fireworks, "_live_model_rows", lambda: [{"id": native} for native in rows.values()])
    monkeypatch.setattr(fireworks, "fetch_provider", lambda **_: ProviderPricingResult(
        slug="fireworks", source="api", fetched_url=fireworks.URL,
        prices={model: ModelPrice(1_000_000, 2_000_000) for model in rows},
    ))
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = fireworks.fetch()
    expected = set(_RETAINED if after_cutoff else rows)
    assert set(result.prices) == expected
    assert set(fireworks._DISCOVERED_MANIFEST_ROWS) == expected


def test_fireworks_september_cold_start_preserves_immutable_deepseek_identity() -> None:
    result = subprocess.run(  # noqa: S603 - fixed local interpreter and literal test program.
        [sys.executable, "-c", "\n".join([
            "from trusted_router.catalog import MODELS, endpoints_for_model",
            "model_id = 'deepseek/deepseek-v4-pro-0813'",
            "assert MODELS[model_id].id == model_id",
            "routes = endpoints_for_model(model_id)",
            "assert routes and all(e.provider != 'fireworks' for e in routes)",
            "assert all(e.model_id == model_id for e in routes)",
            "assert endpoints_for_model('deepseek/deepseek-v4p1-flash')",
        ])],
        env={**os.environ, "TR_ENVIRONMENT": "test", "TR_LIFECYCLE_CLOCK_OVERRIDE": _CUTOFF.isoformat()},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
