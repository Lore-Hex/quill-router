from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing.providers import near_ai
from tests.lifecycle_clock import catalog_predates
from tests.test_near_ai_provider import _catalog_row, _endpoints
from trusted_router import catalog, provider_lifecycle

_CUTOFF = datetime(2026, 9, 11, 13, 0, tzinfo=UTC)
_RETIRING = (("z-ai/glm-5.1", "zai-org/GLM-5.1-FP8"), ("z-ai/glm-5.2", "z-ai/glm-5.2"))
_DSV4 = "deepseek-ai/DeepSeek-V4-Flash"


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING)
def test_near_ai_exact_cutoff_and_provider_scope(model_id: str, native_id: str) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("near-ai", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("near-ai", model_id, at=_CUTOFF)
    assert retired("near-ai", "other-canonical-id", native_id, at=_CUTOFF)
    assert not retired("deepinfra", model_id, native_id, at=_CUTOFF)
    assert not retired("near-ai", "z-ai/glm-5.3-flash", at=_CUTOFF)
    assert not retired("near-ai", "z-ai/glm-5.2-long", at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING)
def test_near_ai_runtime_cutoff_without_catalog_reload(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str
) -> None:
    template = catalog.MODEL_ENDPOINTS["deepseek/deepseek-v4-flash@near-ai/prepaid"]
    endpoint = replace(template, model_id=model_id, upstream_id=native_id)
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {endpoint.id: endpoint})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert catalog.endpoints_for_model(model_id) == [endpoint]
    assert near_ai.canonical_model_id(native_id) == model_id
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert catalog.endpoints_for_model(model_id) == []
    assert near_ai.canonical_model_id(native_id) is None


def test_near_ai_imported_catalog_matches_cutoff() -> None:
    for model_id, _native_id in _RETIRING:
        assert (f"{model_id}@near-ai/prepaid" in catalog.MODEL_ENDPOINTS) is catalog_predates(_CUTOFF)


@pytest.mark.parametrize("after_cutoff", [False, True])
@pytest.mark.parametrize("price_rows_present", [False, True])
def test_near_ai_refresh_cutoff_and_missing_price_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    after_cutoff: bool,
    price_rows_present: bool,
) -> None:
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    retiring_ids = [native for _model, native in _RETIRING]
    # A stale direct registry may still advertise retired endpoints after their
    # pricing rows disappear. Only retired models may bypass that safety gate.
    rows = [_catalog_row(_DSV4), _catalog_row("z-ai/glm-5.3-flash")]
    if price_rows_present:
        rows.extend(_catalog_row(native) for native in retiring_ids)
    endpoints = _endpoints(_DSV4, *retiring_ids)
    endpoints["endpoints"].append({
        "domain": "glm-5-3-flash.completions.near.ai", "models": ["z-ai/glm-5.3-flash"],
    })

    def respond(request: httpx.Request) -> httpx.Response:
        payload = {"data": rows} if str(request.url) == near_ai.CATALOG_URL else endpoints
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("NEAR_API_KEY", "test-key")
    monkeypatch.setattr(near_ai.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(respond))
    monkeypatch.setattr(near_ai, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(near_ai, "UPSTREAM_ID_MAP", dict(near_ai.UPSTREAM_ID_MAP))
    monkeypatch.setattr(near_ai, "MANIFEST_PATH", tmp_path / "near-ai.json")
    if not after_cutoff and not price_rows_present:
        with pytest.raises(RuntimeError, match="endpoint remains published without a pricing row"):
            near_ai.fetch()
        return

    result = near_ai.fetch()
    expected = {"deepseek/deepseek-v4-flash"}
    if not after_cutoff:
        expected.update(model for model, _native in _RETIRING)
    assert set(result.prices) == expected
    assert set(near_ai._DISCOVERED_MANIFEST_ROWS) == expected
    for model_id, native_id in _RETIRING:
        if not after_cutoff:
            assert near_ai._DISCOVERED_MANIFEST_ROWS[model_id]["upstream_id"] == native_id
    # The replacement must pass separate attestation review, not inherit pins
    # from either retired model just because the upstream suggests migration.
    assert "z-ai/glm-5.3-flash" not in result.prices
    near_ai.write_provider_manifest(result)
    manifest = json.loads(near_ai.MANIFEST_PATH.read_text())
    assert {row["id"] for row in manifest["models"]} == expected
