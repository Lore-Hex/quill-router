from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import fireworks
from tests.pinned_manifests import (
    DEEPSEEK_V4_PRO_0813_ROUTES,
    USE_PINNED_MANIFESTS,
    pinned_manifests,
)
from trusted_router import provider_lifecycle

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
# A route Fireworks keeps past the cutoff, as its manifest lists it.
_FIREWORKS_V4P1_FLASH = {
    "id": "deepseek/deepseek-v4p1-flash",
    "upstream_id": _RETAINED["deepseek/deepseek-v4p1-flash"],
    "model_type": "chat",
    "endpoints": ["chat/completions"],
    "context_length": 1048576,
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1200000,
    "cached_input_token_price_per_m": 6000,
}


@pytest.mark.parametrize(("model_id", "native_id"), _RETAINED.items())
def test_fireworks_september_preserves_other_routes(model_id: str, native_id: str) -> None:
    assert not provider_lifecycle.provider_model_retired("fireworks", model_id, native_id, at=_CUTOFF)


def test_fireworks_september_manifest_marks_only_announced_routes() -> None:
    rows = {row["id"]: row for row in json.loads(fireworks.MANIFEST_PATH.read_text())["models"]}
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-09-25T00:00:00Z"
    # A route this notice kept may retire later under a date of its own, as
    # gpt-oss-120b did on 2026-11-26; it never carries this cutoff.
    for model_id in _RETAINED:
        assert rows[model_id].get("retirement_at") != "2026-09-25T00:00:00Z"


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


def test_fireworks_september_cold_start_preserves_immutable_deepseek_identity(
    tmp_path: Path,
) -> None:
    # The 0813 release leaf's routes as they were before the cutoff, and a
    # route Fireworks keeps: the rule holds whatever the hosts list today.
    environ = pinned_manifests(
        tmp_path, (*DEEPSEEK_V4_PRO_0813_ROUTES, ("fireworks", _FIREWORKS_V4P1_FLASH)),
    )
    result = subprocess.run(  # noqa: S603 - fixed local interpreter and literal test program.
        [sys.executable, "-c", USE_PINNED_MANIFESTS + "\n".join([
            "from trusted_router.catalog import MODELS, endpoints_for_model",
            "model_id = 'deepseek/deepseek-v4-pro-0813'",
            "assert MODELS[model_id].id == model_id",
            "routes = endpoints_for_model(model_id)",
            "assert routes and all(e.provider != 'fireworks' for e in routes)",
            "assert all(e.model_id == model_id for e in routes)",
            "assert endpoints_for_model('deepseek/deepseek-v4p1-flash')",
        ])],
        env={**environ, "TR_ENVIRONMENT": "test", "TR_LIFECYCLE_CLOCK_OVERRIDE": _CUTOFF.isoformat()},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
