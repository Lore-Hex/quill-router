"""Fortanix's temporary GLM-5.3-Flash suspension from 2026-11-27 00:00 UTC.

Fortanix's 2026-11-21 notice suspends its GLM-5.3-Flash H200/TDX endpoint for
4-6 weeks while it moves to B200 hardware. The catalog leaves the routes out when built after
the cutover and the request-time filter drops them from a catalog built before; delete
the _Retirement entry (and this module) when Fortanix returns.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException

from tests.catalog_vehicles import registry_endpoints
from tests.lifecycle_clock import catalog_predates
from trusted_router import fortanix_launch
from trusted_router.catalog import MODEL_ENDPOINTS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.fortanix_launch import (
    COMPARISON_MODEL_ID,
    MODEL_ID,
    NATIVE_ID,
    fortanix_launch_offer,
)
from trusted_router.provider_lifecycle import (
    FORTANIX_GLM_53_FLASH_SUSPENSION_AT,
    LIFECYCLE_CLOCK_OVERRIDE_ENV,
    provider_model_retired,
    provider_retirements,
)
from trusted_router.routing import chat_route_endpoint_candidates

INTERNAL_MODEL_ID = "trustedrouter/fortanix-test"
SUSPENDED = (MODEL_ID, INTERNAL_MODEL_ID)
_BEFORE = FORTANIX_GLM_53_FLASH_SUSPENSION_AT - timedelta(seconds=1)
_AT = FORTANIX_GLM_53_FLASH_SUSPENSION_AT


def _stamp(at) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pin(monkeypatch: pytest.MonkeyPatch, at) -> None:
    monkeypatch.setenv(LIFECYCLE_CLOCK_OVERRIDE_ENV, _stamp(at))


def test_cutover_is_midnight_utc_on_november_27() -> None:
    assert _stamp(FORTANIX_GLM_53_FLASH_SUSPENSION_AT) == "2026-11-27T00:00:00Z"


@pytest.mark.parametrize("model_id", SUSPENDED)
def test_suspension_starts_exactly_at_the_cutover(model_id: str) -> None:
    assert not provider_model_retired("fortanix", model_id, NATIVE_ID, at=_BEFORE)
    assert provider_model_retired("fortanix", model_id, NATIVE_ID, at=_AT)
    # The native endpoint ID alone is enough, so no alias of it stays routable.
    assert provider_model_retired("fortanix", "fortanix/any-alias", NATIVE_ID, at=_AT)


def test_suspension_is_scoped_to_fortanix() -> None:
    assert not provider_model_retired("fortanix", "fortanix/other", "Other-Model", at=_AT)
    # Other providers' unrelated retirements may share the 2026-11-27 00:00
    # UTC cutover (DeepInfra's Hy4-preview does), so select every entry at
    # this instant that touches the suspended model, by id or native id.
    entries = [
        retirement
        for retirement in provider_retirements()
        if retirement.effective_at == FORTANIX_GLM_53_FLASH_SUSPENSION_AT
        and (
            NATIVE_ID in retirement.upstream_ids
            or any(model_id in retirement.model_ids for model_id in SUSPENDED)
        )
    ]
    assert [entry.provider for entry in entries] == ["fortanix"]
    # Not an upstream retirement with a substitute: the notice names none.
    assert entries[0].replacement_model_ids == ()


@pytest.mark.parametrize("model_id", SUSPENDED)
def test_registry_carries_the_route_only_if_built_before_the_cutover(model_id: str) -> None:
    # Like other hand-registered routes, the import-time catalog honors it.
    endpoint_id = f"{model_id}@fortanix/prepaid"
    assert (endpoint_id in registry_endpoints()) is catalog_predates(_AT)


@pytest.mark.usefixtures("fortanix_route_live")
@pytest.mark.parametrize("model_id", SUSPENDED)
def test_route_is_served_only_before_the_cutover(
    monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    # A process built before the cutover still drops the route at request time.
    _pin(monkeypatch, _BEFORE)
    assert [endpoint.provider for endpoint in endpoints_for_model(model_id)] == ["fortanix"]
    _pin(monkeypatch, _AT)
    assert endpoints_for_model(model_id) == []


@pytest.mark.usefixtures("fortanix_route_live")
def test_public_model_has_no_route_during_the_suspension(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="test")
    _pin(monkeypatch, _BEFORE)
    before = chat_route_endpoint_candidates({"model": MODEL_ID}, settings)
    assert [endpoint.provider for _model, endpoint in before] == ["fortanix"]
    _pin(monkeypatch, _AT)
    with pytest.raises(HTTPException) as refused:
        chat_route_endpoint_candidates({"model": MODEL_ID}, settings)
    assert refused.value.status_code == 400


@pytest.mark.usefixtures("fortanix_route_live")
def test_launch_offer_turns_off_at_the_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the route gate from the promotion's own end date (Nov 27 Pacific).
    monkeypatch.setattr(fortanix_launch, "_offer_window_open", lambda: True)
    _pin(monkeypatch, _BEFORE)
    assert fortanix_launch_offer()
    _pin(monkeypatch, _AT)
    assert not fortanix_launch_offer()


@pytest.mark.usefixtures("fortanix_route_live")
def test_other_glm_53_flash_routes_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, _BEFORE)
    before = {endpoint.id for endpoint in endpoints_for_model(COMPARISON_MODEL_ID)}
    _pin(monkeypatch, _AT)
    after = {endpoint.id for endpoint in endpoints_for_model(COMPARISON_MODEL_ID)}
    assert before and before == after
    assert all(not endpoint_id.endswith("@fortanix/prepaid") for endpoint_id in after)


@pytest.mark.parametrize("at, exempt", [(_BEFORE, False), (_AT, True)])
def test_price_audit_accepts_a_missing_route_only_during_the_suspension(
    monkeypatch: pytest.MonkeyPatch, at, exempt: bool
) -> None:
    from scripts.check_price_coverage import audit

    endpoint_id = f"{MODEL_ID}@fortanix/prepaid"
    if endpoint_id in MODEL_ENDPOINTS:
        monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    _pin(monkeypatch, at)
    _, info, failures = audit(max_age_days=14, now=at, check_model_discovery=False)
    fortanix_failures = [failure for failure in failures if failure.startswith("fortanix:")]
    assert (fortanix_failures == []) is exempt
    assert any(line.startswith("fortanix: GLM-5.3-Flash suspended") for line in info) is exempt
