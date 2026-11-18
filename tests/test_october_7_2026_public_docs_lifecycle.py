from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from scripts.pricing.providers import scaleway, upstage
from trusted_router import provider_lifecycle


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
def test_public_docs_retirement_spares_the_retained_model(case: Case) -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    assert not provider_lifecycle.provider_model_retired(case.provider, case.retained, at=case.cutoff)


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
