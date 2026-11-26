from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import thinkingmachines
from tests.provider_holds import manifest_rows, row_on_unserved_route_hold
from trusted_router import provider_lifecycle
from trusted_router.catalog_registry import MODEL_ENDPOINTS

# Thinking Machines' Tinker newsletter of 2026-11-25: retiring "on Dec 14", no
# time given, so 00:00 UTC.
_CUTOFF = datetime(2026, 12, 14, 0, 0, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-12-14T00:00:00Z"
_INKLING = ("thinkingmachines/inkling", "thinkingmachines/Inkling:peft:262144:sampling-nvfp4")
# The newsletter's other five retiring models. None has a Tinker route.
_UNROUTED_RETIRING = (
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "qwen/qwen3-8b",
    "qwen/qwen3.5-35b-a3b-base",
    "qwen/qwen3.5-9b-base",
)
# Tinker routes the newsletter does not name. These must keep serving.
_RETAINED = {
    "thinkingmachines/inkling-small": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
    "thinkingmachines/inkling-2-mini": "thinkingmachines/Inkling-2-Mini:peft:262144",
    "thinkingmachines/inkling-2-small": "thinkingmachines/Inkling-2-Small:peft:262144",
    "z-ai/glm-5.3": "zai-org/GLM-5.3:peft:262144",
}
# Kept "through at least March 2027" by the same newsletter.
_KEPT_THROUGH_MARCH_2027 = (
    "z-ai/glm-5.3-flash",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.5-4b",
    "qwen/qwen3.5-9b",
)


def _manifest_rows() -> dict[str, dict]:
    rows = json.loads(thinkingmachines.MANIFEST_PATH.read_text())["models"]
    return {row["id"]: row for row in rows}


def _december_entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "thinkingmachines" and entry.effective_at == _CUTOFF
    ]


def test_cutoff_is_midnight_utc_on_december_14() -> None:
    assert provider_lifecycle.THINKINGMACHINES_DECEMBER_14_RETIREMENT_AT == _CUTOFF


def test_entry_retires_exactly_tinker_inkling_without_a_replacement() -> None:
    (entry,) = _december_entries()
    assert entry.model_ids == frozenset({_INKLING[0]})
    assert entry.upstream_ids == frozenset({_INKLING[1]})
    assert entry.replacement_model_ids == ()
    assert entry.notice_id == f"thinkingmachines:{_CUTOFF.isoformat()}"


def test_manifest_row_carries_the_cutoff() -> None:
    row = _manifest_rows()[_INKLING[0]]
    assert row["upstream_id"] == _INKLING[1]
    assert row["retirement_at"] == _MANIFEST_CUTOFF


def test_inkling_serves_before_the_cutoff_and_retires_at_it() -> None:
    model_id, native_id = _INKLING
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("thinkingmachines", model_id, native_id, at=before)
    assert not provider_lifecycle.provider_model_retired("thinkingmachines", model_id, at=before)
    assert provider_lifecycle.provider_model_retired("thinkingmachines", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired("thinkingmachines", model_id, at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), sorted(_RETAINED.items()))
def test_unnamed_tinker_routes_keep_serving(model_id: str, native_id: str) -> None:
    later = _CUTOFF + timedelta(days=1)
    assert not provider_lifecycle.provider_model_retired("thinkingmachines", model_id, native_id, at=later)
    assert "retirement_at" not in _manifest_rows()[model_id]


@pytest.mark.parametrize("model_id", _KEPT_THROUGH_MARCH_2027 + _UNROUTED_RETIRING)
def test_no_other_tinker_model_is_retired_by_this_notice(model_id: str) -> None:
    assert not any(model_id in entry.model_ids for entry in _december_entries())
    assert f"{model_id}@thinkingmachines/prepaid" not in MODEL_ENDPOINTS


@pytest.mark.parametrize("model_id", _UNROUTED_RETIRING)
def test_other_providers_routes_for_named_models_are_untouched(model_id: str) -> None:
    later = _CUTOFF + timedelta(days=1)
    for endpoint in MODEL_ENDPOINTS.values():
        if endpoint.model_id == model_id:
            assert endpoint.provider != "thinkingmachines"
            # This notice changes nothing for other providers' routes.
            assert provider_lifecycle.provider_model_retired(
                endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
            ) == provider_lifecycle.provider_model_retired(
                endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=_CUTOFF - timedelta(minutes=1),
            ), endpoint.id


def test_tinker_was_inklings_last_host() -> None:
    later = _CUTOFF + timedelta(days=1)
    live = {
        endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == _INKLING[0]
        and not provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
        )
    }
    assert live == set()


def test_inkling_2_small_keeps_its_liberty_hosts() -> None:
    # Liberty 4.1 fuses Inkling 2 Small (different weights from first-gen Inkling).
    later = _CUTOFF + timedelta(days=1)
    live = {
        endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == "thinkingmachines/inkling-2-small"
        and not provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
        )
    }
    # Cloudflare keeps serving it, unless the refresh itself holds that row as unserved.
    cloudflare = {row["id"]: row for row in manifest_rows("cloudflare-workers-ai")}
    assert "cloudflare-workers-ai" in live or row_on_unserved_route_hold(
        cloudflare.get("thinkingmachines/inkling-2-small", {})
    )


_SERVERLESS = [
    {"tinker_id": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
     "input": "$0.30", "cached_input": "$0.06", "output": "$1.20"},
]
_INKLING_ROW = {"tinker_id": _INKLING[1], "input": "$1.00", "cached_input": "$0.17", "output": "$4.05"}
_SAMPLER = [
    {"tinker_id": "zai-org/GLM-5.3:peft:262144", "prefill": "$3.78",
     "cached_prefill": "$0.756", "sample": "$12.15"},
    {"tinker_id": "thinkingmachines/Inkling-2-Small:peft:262144", "prefill": "$0.30",
     "cached_prefill": "$0.03", "sample": "$1.00"},
    {"tinker_id": "thinkingmachines/Inkling-2-Mini:peft:262144", "prefill": "$0.15",
     "cached_prefill": "$0.015", "sample": "$0.50"},
]


@pytest.mark.parametrize("feed_lists_inkling", [False, True])
@pytest.mark.parametrize("after_cutoff", [False, True])
def test_refresh_tolerates_delisting_and_ignores_stale_inkling_after_cutoff(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool, feed_lists_inkling: bool,
) -> None:
    serverless = _SERVERLESS + ([_INKLING_ROW] if feed_lists_inkling else [])
    feeds = {thinkingmachines.SERVERLESS_URL: serverless, thinkingmachines.SAMPLER_URL: _SAMPLER}
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    if not after_cutoff and not feed_lists_inkling:
        with pytest.raises(RuntimeError, match="missing exact model prices"):
            thinkingmachines.fetch()
        return
    result = thinkingmachines.fetch()
    expected = set(_RETAINED) | (set() if after_cutoff else {_INKLING[0]})
    assert set(result.prices) == expected
    assert thinkingmachines._expected_live_models() == expected
