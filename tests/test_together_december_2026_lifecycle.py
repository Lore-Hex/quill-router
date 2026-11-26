from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import together
from tests.provider_holds import manifest_rows, row_on_unserved_route_hold
from trusted_router import provider_lifecycle
from trusted_router.catalog_data import NAMED_DECISION_MODEL_PROVIDERS, TREV_1_0_MODEL_ID
from trusted_router.catalog_registry import MODEL_ENDPOINTS

# Together's notice of 2026-11-25: gpt-oss-120b is deprecated on its serverless
# API "on December 09, 2026", no time given, so 00:00 UTC.
_CUTOFF = datetime(2026, 12, 9, 0, 0, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-12-09T00:00:00Z"
_GPT_OSS_120B = ("openai/gpt-oss-120b", "openai/gpt-oss-120b")
# Together routes the notice does not name, including the alternative it
# suggests (GLM-5.3-Flash). These must keep serving.
_RETAINED = ("z-ai/glm-5.3-flash",)


def _manifest_rows() -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(together.MANIFEST_PATH.read_text())["models"]}


def _december_entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "together" and entry.effective_at == _CUTOFF
    ]


def _live_providers(model_id: str, at: datetime) -> set[str]:
    return {
        endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == model_id and not endpoint.is_byok
        and not provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=at,
        )
    }


def test_cutoff_is_midnight_utc_on_december_9() -> None:
    assert provider_lifecycle.TOGETHER_DECEMBER_9_RETIREMENT_AT == _CUTOFF


def test_entry_retires_exactly_together_gpt_oss_120b_without_a_replacement() -> None:
    # GLM-5.3-Flash, Together's suggestion, is a different model family, so
    # customers' notices do not quote it as a replacement.
    (entry,) = _december_entries()
    assert entry.model_ids == frozenset({_GPT_OSS_120B[0]})
    assert entry.upstream_ids == frozenset({_GPT_OSS_120B[1]})
    assert entry.replacement_model_ids == ()


def test_manifest_row_carries_the_cutoff() -> None:
    row = _manifest_rows()[_GPT_OSS_120B[0]]
    assert row["upstream_id"] == _GPT_OSS_120B[1]
    assert row["retirement_at"] == _MANIFEST_CUTOFF
    assert "replacement_model_id" not in row


def test_together_gpt_oss_120b_serves_before_the_cutoff_and_retires_at_it() -> None:
    model_id, native_id = _GPT_OSS_120B
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("together", model_id, native_id, at=before)
    assert provider_lifecycle.provider_model_retired("together", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize("model_id", _RETAINED)
def test_unnamed_together_routes_keep_serving(model_id: str) -> None:
    # Served after the cutoff, unless the refresh itself holds the row as unserved.
    assert "together" in _live_providers(
        model_id, _CUTOFF + timedelta(days=1)
    ) or row_on_unserved_route_hold(_manifest_rows().get(model_id, {}))


def test_other_providers_keep_gpt_oss_120b() -> None:
    # Clock-independent: the catalog drops Together's route itself once the
    # cutoff has passed, so compare the other hosts on either side of it.
    before = _live_providers(_GPT_OSS_120B[0], _CUTOFF - timedelta(minutes=1))
    after = _live_providers(_GPT_OSS_120B[0], _CUTOFF + timedelta(days=1))
    assert "together" not in after
    assert after == before - {"together"}
    assert len(after) >= 2


def test_trev_keeps_live_hosts_on_its_chain_after_the_cutoff() -> None:
    # Together is the last host on trev-1.0's chain; the name keeps serving on
    # the hosts ahead of it.
    chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    assert "together" in chain
    live = _live_providers(_GPT_OSS_120B[0], _CUTOFF + timedelta(days=1))
    # Cerebras keeps serving it, unless the refresh itself holds that row as unserved.
    cerebras = {row["id"]: row for row in manifest_rows("cerebras")}
    assert "cerebras" in live or row_on_unserved_route_hold(cerebras.get(_GPT_OSS_120B[0], {}))
    assert [host for host in chain if host in live]


def test_same_day_inkling_notice_needs_no_entry() -> None:
    # Together's same-day Inkling notice: its Inkling route already retired on
    # 2026-11-06 and is no longer in the catalog.
    assert provider_lifecycle.provider_model_retired(
        "together", "thinkingmachines/inkling", "thinkingmachines/Inkling",
        at=provider_lifecycle.TOGETHER_NOVEMBER_6_RETIREMENT_AT,
    )
    assert not any(
        endpoint.provider == "together" and endpoint.model_id.startswith("thinkingmachines/inkling")
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert not any("thinkingmachines/inkling" in entry.model_ids for entry in _december_entries())
