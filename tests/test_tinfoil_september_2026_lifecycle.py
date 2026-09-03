from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing.providers import tinfoil
from tests.lifecycle_clock import catalog_predates
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS

_CUTOFF = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
_GLM52 = "z-ai/glm-5.2"
_GLM52_UPSTREAM = "glm-5-2"


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "provider": "tinfoil",
                "models": [
                    {
                        "id": _GLM52,
                        "upstream_id": _GLM52_UPSTREAM,
                        "display_name": "GLM-5.2",
                        "title": _GLM52,
                        "model_type": "chat",
                        "features": ["reasoning", "function-calling"],
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "endpoints": ["chat/completions", "responses"],
                        "status": 1,
                        "context_length": 1_048_576,
                        "input_token_price_per_m": 1_500_000,
                        "cached_input_token_price_per_m": 375_000,
                        "output_token_price_per_m": 5_250_000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_tinfoil_glm52_retires_at_announced_cutoff() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "tinfoil",
        _GLM52,
        _GLM52_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "tinfoil",
        _GLM52,
        _GLM52_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "parasail",
        _GLM52,
        _GLM52_UPSTREAM,
        at=_CUTOFF,
    )


def test_tinfoil_catalog_matches_glm52_cutover_state() -> None:
    prepaid_id = f"{_GLM52}@tinfoil/prepaid"
    byok_id = f"{_GLM52}@tinfoil/byok"
    expected_present = catalog_predates(_CUTOFF)

    assert (prepaid_id in MODEL_ENDPOINTS) is expected_present
    assert (byok_id in MODEL_ENDPOINTS) is expected_present
    assert "z-ai/glm-5.3@tinfoil/prepaid" in MODEL_ENDPOINTS
    assert "z-ai/glm-5.3-flash@tinfoil/prepaid" in MODEL_ENDPOINTS


def test_tinfoil_preserves_early_feed_removal_until_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: {"data": []})
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )

    result = tinfoil.fetch()

    preserved = result.prices[_GLM52]
    assert preserved.prompt_micro_per_m == 1_500_000
    assert preserved.completion_micro_per_m == 5_250_000
    assert preserved.tiers[0].prompt_cached_micro_per_m == 375_000
    assert tinfoil._DISCOVERED_MANIFEST_ROWS[_GLM52]["context_length"] == 1_048_576
    assert any("preserved z-ai/glm-5.2" in note for note in result.notes)


def test_tinfoil_drops_preserved_route_at_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: {"data": []})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    result = tinfoil.fetch()

    assert _GLM52 not in result.prices
    assert _GLM52 not in tinfoil._DISCOVERED_MANIFEST_ROWS
    assert not any(
        _GLM52 in note for note in result.notes if note.startswith("validation notes")
    )


def test_tinfoil_live_feed_cannot_restore_glm52_after_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    _write_manifest(manifest_path)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    monkeypatch.setattr(
        tinfoil,
        "fetch_json",
        lambda _url: {
            "data": [
                {
                    "id": _GLM52_UPSTREAM,
                    "type": "chat",
                    "endpoints": ["/v1/chat/completions"],
                    "pricing": {
                        "inputTokenPricePer1M": 1.5,
                        "outputTokenPricePer1M": 5.25,
                    },
                }
            ]
        },
    )

    result = tinfoil.fetch()

    assert _GLM52 not in result.prices
    assert _GLM52 not in tinfoil._DISCOVERED_MANIFEST_ROWS


def test_tinfoil_preservation_keeps_existing_route_hold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    _write_manifest(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["models"][0].update(
        {
            "routable": False,
            "routable_reason": "provider-canary-failed",
            "unresolved_since": "2026-09-03",
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: {"data": []})
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )

    result = tinfoil.fetch()
    tinfoil.write_provider_manifest(result)

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    preserved = next(row for row in written["models"] if row["id"] == _GLM52)
    assert preserved["routable"] is False
    assert preserved["routable_reason"] == "provider-canary-failed"
    assert preserved["unresolved_since"] == "2026-09-03"
