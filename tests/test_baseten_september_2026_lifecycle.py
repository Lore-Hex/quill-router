from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.pricing.providers import baseten
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 9, 26, 0, tzinfo=UTC)
_RETIRING = {
    "z-ai/glm-4.7": "zai-org/GLM-4.7",
    "moonshotai/kimi-k2.7-code": "moonshotai/Kimi-K2.7-Code",
    "moonshotai/kimi-k2.6": "moonshotai/Kimi-K2.6",
    "thinkingmachines/inkling-1m": "thinkingmachines/inkling",
    "thinkingmachines/inkling-small": "thinkingmachines/inkling-small",
    "deepseek/deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
}
_RETAINED = {
    "z-ai/glm-5.2": "zai-org/GLM-5.2",
    "z-ai/glm-5.2-fast": "zai-org/GLM-5.2-Fast",
    "z-ai/glm-5.3": "zai-org/GLM-5.3",
    "z-ai/glm-5.3-flash": "zai-org/GLM-5.3-Flash",
    "z-ai/glm-5.3-fast": "zai-org/GLM-5.3-Fast",
    "moonshotai/kimi-k3": "moonshotai/Kimi-K3",
    "deepseek/deepseek-v4-pro-0813": "deepseek-ai/DeepSeek-V4-Pro-0813",
    "deepseek/deepseek-v4-flash-0731": "deepseek-ai/DeepSeek-V4-Flash-0731",
}


def test_baseten_september_cutoff_matches_pacific_notice() -> None:
    announced = datetime(2026, 9, 25, 17, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert announced.astimezone(UTC) == _CUTOFF
    assert provider_lifecycle.BASETEN_SEPTEMBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model_id", "native_id"), _RETAINED.items())
def test_baseten_september_preserves_unannounced_models(model_id: str, native_id: str) -> None:
    assert not provider_lifecycle.provider_model_retired("baseten", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_baseten_september_stale_feed_cannot_rediscover_retired_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    payload = {"data": [
        {"id": native, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
        for native in (_RETIRING | _RETAINED).values()
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
    expected = set(_RETAINED) if after_cutoff else set(_RETIRING | _RETAINED)
    assert set(result.prices) == expected
    assert set(baseten._DISCOVERED_MANIFEST_ROWS) == expected
    baseten.write_provider_manifest(result)
    rows = json.loads(baseten.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected


def test_baseten_september_manifest_records_only_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-09-26T00:00:00Z"
    for model_id in _RETAINED:
        # Some later retire in Baseten's December notice, never in September's.
        assert rows[model_id].get("retirement_at") != "2026-09-26T00:00:00Z"
