from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import alibaba, deepinfra, novita, scaleway, xiaomi
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@dataclass(frozen=True)
class Case:
    provider: str
    model: str
    native: str
    cutoff: datetime
    replacement: str


_CASES = [
    Case("novita", "xiaomimimo/mimo-v2.5", "xiaomimimo/mimo-v2.5",
         datetime(2026, 10, 23, tzinfo=UTC), "xiaomi/mimo-v2.6-flash"),
    Case("novita", "xiaomimimo/mimo-v2.5-pro", "xiaomimimo/mimo-v2.5-pro",
         datetime(2026, 10, 23, tzinfo=UTC), "xiaomi/mimo-v2.6-pro"),
    Case("deepinfra", "nousresearch/hermes-3-llama-3.1-405b", "NousResearch/Hermes-3-Llama-3.1-405B",
         datetime(2026, 11, 2, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("alibaba", "qwen/qwen3.5-plus", "qwen3.5-plus",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("alibaba", "qwen/qwen3.5-plus-2026-02-15", "qwen3.5-plus-2026-02-15",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("alibaba", "qwen/qwen3.5-397b-a17b", "qwen3.5-397b-a17b",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("deepinfra", "openai/gpt-oss-120b-turbo", "openai/gpt-oss-120b-Turbo",
         datetime(2026, 10, 20, tzinfo=UTC), "openai/gpt-oss-120b"),
    Case("scaleway", "z-ai/glm-5.2", "glm-5.2",
         datetime(2026, 11, 16, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("deepinfra", "thinkingmachines/inkling", "thinkingmachines/Inkling",
         datetime(2026, 10, 22, tzinfo=UTC), "thinkingmachines/inkling-small"),
    Case("deepinfra", "ibm-granite/granite-4.2-3b", "ibm-granite/granite-4.2-3b",
         datetime(2026, 10, 18, tzinfo=UTC), "ibm-granite/granite-4.2-8b"),
    Case("deepinfra", "ibm-granite/granite-4.2-30b", "ibm-granite/granite-4.2-30b",
         datetime(2026, 10, 18, tzinfo=UTC), "ibm-granite/granite-4.2-8b"),
    Case("deepinfra", "qwen/qwen3-max", "Qwen/Qwen3-Max",
         datetime(2026, 10, 10, tzinfo=UTC), "qwen/qwen3.8-max"),
    Case("deepinfra", "qwen/qwen3-max-thinking", "Qwen/Qwen3-Max-Thinking",
         datetime(2026, 10, 10, tzinfo=UTC), "qwen/qwen3.8-max"),
    Case("xiaomi", "xiaomi/mimo-v2.5-pro", "mimo-v2.5-pro",
         datetime(2026, 10, 14, 10, tzinfo=UTC), "xiaomi/mimo-v2.6-pro"),
    Case("xiaomi", "xiaomi/mimo-v2.5", "mimo-v2.5",
         datetime(2026, 10, 14, 10, tzinfo=UTC), "xiaomi/mimo-v2.6-flash"),
]
_MANIFESTS = {
    "novita": novita.MANIFEST_PATH,
    "alibaba": alibaba.MANIFEST_PATH,
    "deepinfra": deepinfra.MANIFEST_PATH,
    "scaleway": scaleway.MANIFEST_PATH,
    "xiaomi": xiaomi.MANIFEST_PATH,
}


def test_xiaomi_cutoff_is_the_substitution_start_not_the_shutoff() -> None:
    # 18:00 in UTC+8 on October 14; the IDs only error from October 21.
    assert provider_lifecycle.XIAOMI_MIMO_V25_RETIREMENT_AT == datetime(2026, 10, 14, 10, tzinfo=UTC)
    assert provider_lifecycle.DEEPINFRA_QWEN3_MAX_THINKING_RETIREMENT_AT == datetime(
        2026, 10, 10, tzinfo=UTC,
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_route_routed_before_substitution_retired_after(
    monkeypatch: pytest.MonkeyPatch, case: Case,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    before = case.cutoff - timedelta(microseconds=1)
    assert not retired(case.provider, case.model, case.native, at=before)
    assert retired(case.provider, case.model, at=case.cutoff)
    assert retired(case.provider, "unknown-canonical", case.native, at=case.cutoff)
    assert not retired("unaffected-provider", case.model, case.native, at=case.cutoff)
    assert not retired(case.provider, case.replacement, at=case.cutoff)
    endpoints = {}
    for slug in (case.provider, "unaffected-provider"):
        endpoint = ModelEndpoint(
            id=f"{case.model}@{slug}/Credits", model_id=case.model, provider=slug,
            usage_type="Credits", upstream_id=case.native,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: before)
    assert len(catalog.endpoints_for_model(case.model)) == 2
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    assert [row.provider for row in catalog.endpoints_for_model(case.model)] == [
        "unaffected-provider",
    ]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_replacement_is_quoted_and_manifest_annotated(case: Case) -> None:
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == case.provider and case.model in entry.model_ids
        and entry.effective_at == case.cutoff
    ]
    assert len(entries) == 1
    assert entries[0].replacement_model_ids == (case.replacement,)
    rows = {row["id"]: row for row in json.loads(_MANIFESTS[case.provider].read_text())["models"]}
    assert rows[case.model]["upstream_id"] == case.native
    assert rows[case.model]["retirement_at"] == case.cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rows[case.model]["replacement_model_id"] == case.replacement
