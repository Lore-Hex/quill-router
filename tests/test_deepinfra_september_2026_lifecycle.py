from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import deepinfra
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@dataclass(frozen=True)
class RetirementCase:
    model: str
    upstream: str
    replacement: str
    replacement_upstream: str
    cutoff: datetime


@pytest.fixture(params=[
    RetirementCase(
        "moonshotai/kimi-k2.5", "moonshotai/Kimi-K2.5",
        "moonshotai/kimi-k3", "moonshotai/Kimi-K3",
        datetime(2026, 9, 7, tzinfo=UTC),
    ),
    RetirementCase(
        "z-ai/glm-4.7-flash", "zai-org/GLM-4.7-Flash",
        "z-ai/glm-5.3-flash", "zai-org/GLM-5.3-Flash",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
    RetirementCase(
        "z-ai/glm-5", "zai-org/GLM-5",
        "z-ai/glm-5.2", "zai-org/GLM-5.2",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
    RetirementCase(
        "minimax/minimax-m2.7", "MiniMaxAI/MiniMax-M2.7",
        "minimax/minimax-m3", "MiniMaxAI/MiniMax-M3",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
], ids=lambda case: case.model)
def retirement(request: pytest.FixtureRequest) -> RetirementCase:
    return request.param


def test_deepinfra_exact_cutoff_and_provider_scope(retirement: RetirementCase) -> None:
    case = retirement
    retired = provider_lifecycle.provider_model_retired
    assert not retired(
        "deepinfra", case.model, case.upstream,
        at=case.cutoff - timedelta(microseconds=1),
    )
    assert retired("deepinfra", case.model, at=case.cutoff)
    assert retired("deepinfra", "other-canonical-id", case.upstream, at=case.cutoff)
    assert not retired(
        "deepinfra", case.replacement, case.replacement_upstream, at=case.cutoff,
    )
    for provider in ("novita", "siliconflow"):
        assert not retired(provider, case.model, case.upstream, at=case.cutoff)


def test_deepinfra_runtime_cutoff_without_catalog_reload(
    monkeypatch: pytest.MonkeyPatch, retirement: RetirementCase,
) -> None:
    case = retirement
    # Discovery has already delisted some models. Fixture live endpoints to
    # prove the clock gate also protects a process with a stale catalog.
    endpoints = {}
    for provider in ("deepinfra", "novita"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{case.model}@{provider}/{usage}", model_id=case.model,
                provider=provider, usage_type=usage, upstream_id=case.upstream,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: case.cutoff - timedelta(microseconds=1),
    )
    assert len(catalog.endpoints_for_model(case.model)) == 4

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    after = catalog.endpoints_for_model(case.model)
    assert len(after) == 2
    assert {endpoint.provider for endpoint in after} == {"novita"}
    assert {endpoint.model_id for endpoint in after} == {case.model}


def test_hourly_refresh_cannot_restore_retired_deepinfra_model(
    monkeypatch: pytest.MonkeyPatch, retirement: RetirementCase,
) -> None:
    case = retirement
    result = ProviderPricingResult(
        slug="deepinfra",
        prices={
            case.model: ModelPrice(450_000, 2_250_000),
            case.replacement: ModelPrice(2_850_000, 14_250_000),
        },
        source="api", fetched_url=deepinfra.URL,
    )
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: case.cutoff - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"deepinfra": result})
    assert "deepinfra" in before[case.model]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    after = refresh._index_provider_prices({"deepinfra": result})
    assert case.model not in after
    assert "deepinfra" in after[case.replacement]


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_deepinfra_parser_and_manifest_filter_stale_feed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    retirement: RetirementCase, after_cutoff: bool,
) -> None:
    case = retirement
    payload = {"data": [
        {"id": native, "metadata": {"pricing": {"input_tokens": 0.45, "output_tokens": 2.25}}}
        for native in (case.upstream, case.replacement_upstream)
    ]}
    monkeypatch.setattr(
        deepinfra.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(deepinfra, "UPSTREAM_ID_MAP", dict(deepinfra.UPSTREAM_ID_MAP))
    monkeypatch.setattr(deepinfra, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(deepinfra, "MANIFEST_PATH", tmp_path / "deepinfra.json")
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: case.cutoff if after_cutoff else case.cutoff - timedelta(microseconds=1),
    )
    result = deepinfra.fetch()
    expected = {case.replacement}
    if not after_cutoff:
        expected.add(case.model)
        assert deepinfra._DISCOVERED_MANIFEST_ROWS[case.model]["upstream_id"] == case.upstream
    assert set(result.prices) == expected
    assert set(deepinfra._DISCOVERED_MANIFEST_ROWS) == expected
    deepinfra.write_provider_manifest(result)
    rows = json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected


def test_deepinfra_manifest_records_announced_replacement(retirement: RetirementCase) -> None:
    case = retirement
    rows = {row["id"]: row for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]}
    assert rows[case.model]["retirement_at"] == case.cutoff.isoformat().replace("+00:00", "Z")
    assert rows[case.model]["replacement_model_id"] == case.replacement
    assert rows[case.model]["upstream_id"] == case.upstream


def test_deepinfra_retirement_does_not_include_unannounced_turbo_variant() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra", "minimax/minimax-m2.7-turbo", "MiniMaxAI/MiniMax-M2.7-Turbo",
        at=datetime(2026, 9, 10, tzinfo=UTC),
    )
