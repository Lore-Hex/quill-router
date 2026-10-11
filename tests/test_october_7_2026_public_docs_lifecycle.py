from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import scaleway, upstage
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@dataclass(frozen=True)
class Case:
    provider: str
    model: str
    native: str
    cutoff: datetime
    retained: str


_CASES = [
    Case("scaleway", "mistralai/pixtral-12b-2409", "pixtral-12b-2409",
         datetime(2026, 10, 7, tzinfo=UTC), "meta-llama/llama-3.3-70b-instruct"),
    Case("scaleway", "qwen/qwen3-coder-30b-a3b-instruct", "qwen3-coder-30b-a3b-instruct",
         datetime(2026, 10, 7, tzinfo=UTC), "qwen/qwen3-235b-a22b-instruct-2507"),
    # October 30 00:00 KST is October 29 15:00 UTC.
    Case("upstage", "upstage/solar-pro2", "solar-pro2",
         datetime(2026, 10, 29, 15, tzinfo=UTC), "upstage/solar-pro4"),
    Case("upstage", "upstage/solar-pro3", "solar-pro3",
         datetime(2026, 10, 29, 15, tzinfo=UTC), "upstage/solar-pro4"),
]
_MANIFESTS = {"scaleway": scaleway.MANIFEST_PATH, "upstage": upstage.MANIFEST_PATH}


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_public_docs_retirement_boundary_and_provider_scope(case: Case) -> None:
    retired = provider_lifecycle.provider_model_retired
    before = case.cutoff - timedelta(microseconds=1)
    assert not retired(case.provider, case.model, case.native, at=before)
    assert retired(case.provider, case.model, at=case.cutoff)
    assert retired(case.provider, "different-canonical-id", case.native, at=case.cutoff)
    assert not retired("unaffected-provider", case.model, case.native, at=case.cutoff)
    assert not retired(case.provider, case.retained, at=case.cutoff)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_public_docs_retirement_filters_existing_routes_without_substitution(
    monkeypatch: pytest.MonkeyPatch, case: Case,
) -> None:
    endpoints = {}
    for provider in (case.provider, "unaffected-provider"):
        endpoint = ModelEndpoint(
            id=f"{case.model}@{provider}/Credits", model_id=case.model,
            provider=provider, usage_type="Credits", upstream_id=case.native,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(case.model)) == 2
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    assert [(row.provider, row.model_id, row.upstream_id)
            for row in catalog.endpoints_for_model(case.model)] == [
        ("unaffected-provider", case.model, case.native),
    ]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_public_docs_retirement_notice_matches_manifest_ids(case: Case) -> None:
    rows = {row["id"]: row for row in json.loads(_MANIFESTS[case.provider].read_text())["models"]}
    assert rows[case.model]["upstream_id"] == case.native
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == case.provider and case.model in entry.model_ids
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert case.native in entry.upstream_ids
    assert entry.effective_at == case.cutoff
    assert entry.notice_id == f"{case.provider}:{case.cutoff.isoformat()}"
    assert entry.replacement_model_ids == ()
