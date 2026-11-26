from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from scripts.pricing.providers import deepinfra
from trusted_router import provider_lifecycle
from trusted_router.catalog_registry import MODEL_ENDPOINTS

# DeepInfra's notice of 2026-11-26: ibm-granite/granite-4.2-8b "is being
# deprecated on 2026-12-03", no time given, so 00:00 UTC. Requests are then
# redirected to Qwen/Qwen3.5-9B, a different model, so the route must retire
# at the cutoff rather than keep answering under the Granite id.
_CUTOFF = datetime(2026, 12, 3, 0, 0, tzinfo=UTC)
_MANIFEST_CUTOFF = "2026-12-03T00:00:00Z"
_GRANITE_8B = ("ibm-granite/granite-4.2-8b", "ibm-granite/granite-4.2-8b")
_REPLACEMENT = "qwen/qwen3.5-9b"


def _manifest_rows() -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]}


def _december_3_entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "deepinfra" and entry.effective_at == _CUTOFF
    ]


def _live_providers(model_id: str, at: datetime) -> set[str]:
    return {
        endpoint.provider for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == model_id and not endpoint.is_byok
        and not provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=at,
        )
    }


def test_cutoff_is_midnight_utc_on_december_3() -> None:
    assert provider_lifecycle.DEEPINFRA_GRANITE_42_8B_RETIREMENT_AT == _CUTOFF


def test_entry_retires_exactly_deepinfra_granite_8b_and_quotes_the_redirect_target() -> None:
    (entry,) = _december_3_entries()
    assert entry.model_ids == frozenset({_GRANITE_8B[0]})
    assert entry.upstream_ids == frozenset({_GRANITE_8B[1]})
    assert entry.replacement_model_ids == (_REPLACEMENT,)


def test_manifest_row_carries_the_cutoff_and_replacement() -> None:
    row = _manifest_rows()[_GRANITE_8B[0]]
    assert row["upstream_id"] == _GRANITE_8B[1]
    assert row["retirement_at"] == _MANIFEST_CUTOFF
    assert row["replacement_model_id"] == _REPLACEMENT


def test_deepinfra_granite_8b_serves_before_the_cutoff_and_retires_at_it() -> None:
    model_id, native_id = _GRANITE_8B
    before = _CUTOFF - timedelta(minutes=1)
    assert not provider_lifecycle.provider_model_retired("deepinfra", model_id, native_id, at=before)
    assert provider_lifecycle.provider_model_retired("deepinfra", model_id, native_id, at=_CUTOFF)
    assert provider_lifecycle.provider_model_retired(
        "deepinfra", model_id, native_id, at=_CUTOFF + timedelta(days=30),
    )


def test_wandb_keeps_granite_8b() -> None:
    # Clock-independent: the catalog drops DeepInfra's route itself once the
    # cutoff has passed, so compare the other hosts on either side of it.
    before = _live_providers(_GRANITE_8B[0], _CUTOFF - timedelta(minutes=1))
    after = _live_providers(_GRANITE_8B[0], _CUTOFF + timedelta(days=1))
    assert "deepinfra" not in after
    assert after == before - {"deepinfra"}
    assert "wandb" in after


def test_replacement_is_in_the_catalog_and_deepinfra_keeps_serving_it() -> None:
    # The quoted replacement is the model DeepInfra redirects to; it stays
    # routable on DeepInfra and elsewhere after the cutoff.
    assert "deepinfra" in _live_providers(_REPLACEMENT, _CUTOFF + timedelta(days=1))
