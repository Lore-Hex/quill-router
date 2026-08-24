from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import parasail
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model
from trusted_router.synthetic.probes import rotation_candidates

_CUTOFF = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
_RETIRING = (
    ("z-ai/glm-5", "zai-org/GLM-5-FP8"),
    ("minimax/minimax-m2.5", "MiniMaxAI/MiniMax-M2.5"),
    (
        "qwen/qwen3-235b-a22b-2507",
        "Qwen/Qwen3-235B-A22B-Instruct-2507",
    ),
)
_REPLACEMENTS = (
    ("z-ai/glm-5.2", "parasail-glm-52"),
    ("minimax/minimax-m3", "parasail-minimax-m3"),
    ("qwen/qwen3.5-397b-a17b", "parasail-qwen35-397b-a17b"),
)


def test_parasail_retirements_switch_at_announced_date() -> None:
    before = _CUTOFF - timedelta(microseconds=1)

    for model_id, upstream_id in _RETIRING:
        assert not provider_lifecycle.provider_model_retired(
            "parasail", model_id, upstream_id, at=before
        )
        assert provider_lifecycle.provider_model_retired(
            "parasail", model_id, upstream_id, at=_CUTOFF
        )

    for model_id, upstream_id in _REPLACEMENTS:
        assert not provider_lifecycle.provider_model_retired(
            "parasail", model_id, upstream_id, at=_CUTOFF
        )


def test_parasail_retirements_are_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    for model_id, _upstream_id in _RETIRING:
        providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
        assert "parasail" not in providers
        assert providers, f"{model_id} should remain available outside Parasail"

    for model_id, upstream_id in _REPLACEMENTS:
        parasail_routes = [
            endpoint
            for endpoint in endpoints_for_model(model_id)
            if endpoint.provider == "parasail"
        ]
        assert parasail_routes
        assert {endpoint.upstream_id for endpoint in parasail_routes} == {upstream_id}


def test_hourly_refresh_cannot_restore_retired_parasail_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = {
        model_id: ModelPrice(100_000, 200_000) for model_id, _upstream_id in _RETIRING
    }
    prices.update(
        {model_id: ModelPrice(300_000, 1_200_000) for model_id, _ in _REPLACEMENTS}
    )
    result = ProviderPricingResult(
        slug="parasail",
        prices=prices,
        source="api",
        fetched_url=parasail.PRICING_URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"parasail": result})
    assert all("parasail" in before[model_id] for model_id, _ in _RETIRING)

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"parasail": result})
    assert all(model_id not in after for model_id, _ in _RETIRING)
    assert all("parasail" in after[model_id] for model_id, _ in _REPLACEMENTS)


def test_parasail_parser_drops_retired_models_from_stale_feeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display_by_model = {
        "z-ai/glm-5": "GLM-5",
        "minimax/minimax-m2.5": "MiniMax M2.5",
        "qwen/qwen3-235b-a22b-2507": "Qwen3 235B-A22B (2507)",
        "z-ai/glm-5.2": "GLM-5.2",
        "minimax/minimax-m3": "MiniMax M3",
        "qwen/qwen3.5-397b-a17b": "Qwen3.5 397B-A17B",
    }
    page_rows = {
        display: (0.10, 0.20, None) for display in display_by_model.values()
    }
    live_rows = [{"id": upstream_id} for _model_id, upstream_id in _RETIRING]
    live_rows.extend({"id": upstream_id} for _model_id, upstream_id in _REPLACEMENTS)

    class FakeResponse:
        text = "unused"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": live_rows}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(parasail, "_http_client", FakeClient)
    monkeypatch.setattr(parasail, "_parse_pricing_page", lambda _html: (page_rows, []))

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = parasail.fetch()
    assert all(model_id in before.prices for model_id, _ in _RETIRING)

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = parasail.fetch()
    assert all(model_id not in after.prices for model_id, _ in _RETIRING)
    assert all(model_id in after.prices for model_id, _ in _REPLACEMENTS)


def test_parasail_manifest_prunes_retired_rows_at_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": model_id, "upstream_id": upstream_id}
        for model_id, upstream_id in (*_RETIRING, *_REPLACEMENTS)
    ]
    # The manifest writer requires every first-party supplemental row to be
    # refreshed. Include Kimi K2.7 Code, which is unrelated to this retirement.
    rows.append(
        {
            "id": "moonshotai/kimi-k2.7-code",
            "upstream_id": "parasail-kimi-k27-code",
        }
    )
    path = tmp_path / "parasail.json"
    path.write_text(json.dumps({"provider": "parasail", "models": rows}), encoding="utf-8")
    monkeypatch.setattr(parasail, "MANIFEST_PATH", path)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    active_prices = {
        model_id: ModelPrice(300_000, 1_200_000) for model_id, _ in _REPLACEMENTS
    }
    active_prices["moonshotai/kimi-k2.7-code"] = ModelPrice(750_000, 3_500_000)
    parasail.write_provider_manifest(
        ProviderPricingResult(
            slug="parasail",
            prices=active_prices,
            source="api",
            fetched_url=parasail.PRICING_URL,
        )
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    model_ids = {row["id"] for row in saved["models"]}
    assert not model_ids & {model_id for model_id, _ in _RETIRING}
    assert {model_id for model_id, _ in _REPLACEMENTS} <= model_ids


def test_synthetic_rotation_excludes_retired_parasail_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    parasail_pool = set(rotation_candidates()["parasail"])

    assert not parasail_pool & {model_id for model_id, _ in _RETIRING}
    assert {model_id for model_id, _ in _REPLACEMENTS} <= parasail_pool
