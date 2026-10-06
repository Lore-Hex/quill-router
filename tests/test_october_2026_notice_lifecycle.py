from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import deepinfra, google_vertex
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@dataclass(frozen=True)
class RetirementCase:
    provider: str
    model: str
    native: str
    cutoff: datetime
    replacements: tuple[str, ...]


_CASES = [
    RetirementCase(
        "deepinfra", "inclusionai/ling-3.0-flash-fin", "inclusionAI/Ling-3.0-flash-Fin",
        datetime(2026, 10, 9, tzinfo=UTC), ("inclusionai/ling-3.0-flash-vl",),
    ),
    RetirementCase(
        "google-vertex", "google/gemini-3.6-flash", "gemini-3.6-flash",
        datetime(2026, 11, 19, tzinfo=UTC),
        ("google/gemini-3.8-flash", "google/gemini-3.5-flash"),
    ),
    RetirementCase(
        "google-vertex", "google/gemini-3.7-flash", "gemini-3.7-flash",
        datetime(2027, 1, 28, tzinfo=UTC),
        ("google/gemini-3.8-flash", "google/gemini-3.5-flash"),
    ),
]


@pytest.fixture(params=_CASES, ids=lambda case: case.model)
def retirement(request: pytest.FixtureRequest) -> RetirementCase:
    return request.param


def test_cutoffs_are_conservative_for_date_only_notices() -> None:
    assert provider_lifecycle.DEEPINFRA_LING_30_FLASH_FIN_RETIREMENT_AT == _CASES[0].cutoff
    assert provider_lifecycle.GOOGLE_VERTEX_GEMINI_36_FLASH_RETIREMENT_AT == _CASES[1].cutoff
    assert provider_lifecycle.GOOGLE_VERTEX_GEMINI_37_FLASH_RETIREMENT_AT == _CASES[2].cutoff


def test_retirement_boundary_and_provider_scope(retirement: RetirementCase) -> None:
    retired = provider_lifecycle.provider_model_retired
    case = retirement
    assert not retired(case.provider, case.model, case.native, at=case.cutoff - timedelta(microseconds=1))
    assert retired(case.provider, case.model, at=case.cutoff)
    assert retired(case.provider, case.model, case.native, at=case.cutoff + timedelta(days=1))
    for other in ("gmi", "google-ai-studio", "unaffected-provider"):
        assert not retired(other, case.model, case.native, at=case.cutoff)


def test_routed_before_cutover_retired_after_without_substitution(
    monkeypatch: pytest.MonkeyPatch, retirement: RetirementCase,
) -> None:
    case = retirement
    endpoints = {}
    for provider in (case.provider, "unaffected-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{case.model}@{provider}/{usage}", model_id=case.model,
                provider=provider, usage_type=usage, upstream_id=case.native,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(case.model)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    remaining = catalog.endpoints_for_model(case.model)
    assert {endpoint.provider for endpoint in remaining} == {"unaffected-provider"}
    assert {endpoint.model_id for endpoint in remaining} == {case.model}
    assert {endpoint.upstream_id for endpoint in remaining} == {case.native}


def test_replacements_are_quoted_not_substituted(retirement: RetirementCase) -> None:
    case = retirement
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == case.provider and case.model in entry.model_ids
    ]
    assert len(entries) == 1
    assert entries[0].effective_at == case.cutoff
    assert entries[0].replacement_model_ids == case.replacements
    for replacement in case.replacements:
        assert not provider_lifecycle.provider_model_retired(
            case.provider, replacement, at=case.cutoff,
        )


def test_neighbouring_models_survive() -> None:
    retired = provider_lifecycle.provider_model_retired
    ling = datetime(2026, 10, 9, tzinfo=UTC)
    assert not retired("deepinfra", "inclusionai/ling-3.0-flash", "inclusionAI/Ling-3.0-flash", at=ling)
    assert not retired("deepinfra", "inclusionai/ling-3.0-flash-vl", "inclusionAI/Ling-3.0-flash-VL", at=ling)
    # Only Vertex's notice arrived: DeepInfra's Gemini 3.7 Flash route stays.
    late = datetime(2027, 1, 28, tzinfo=UTC)
    assert not retired("deepinfra", "google/gemini-3.7-flash", "google/gemini-3.7-flash", at=late)
    assert not retired("google-vertex", "google/gemini-3.8-flash", "gemini-3.8-flash", at=late)


def test_manifests_record_the_announced_retirements() -> None:
    manifests = {"deepinfra": deepinfra.MANIFEST_PATH, "google-vertex": google_vertex.MANIFEST_PATH}
    for case in _CASES:
        rows = {row["id"]: row for row in json.loads(manifests[case.provider].read_text())["models"]}
        row = rows[case.model]
        assert row["upstream_id"] == case.native
        assert row["retirement_at"] == case.cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert row["replacement_model_id"] == case.replacements[0]
