"""Real authorize/settle contracts for non-heartbeating estimated-usage routes."""
from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from tests.test_settle_one_commit import INTERNAL, _settle_body
from tests.test_settle_one_commit import fixed_catalog as fixed_catalog
from tests.test_settle_one_commit import prod_store as prod_store
from tests.test_snapshot_billing import refresh
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.stage_d import (
    ESTIMATED_USAGE_SNAPSHOT_REASON,
    billing_pricing_snapshot,
)


@pytest.mark.parametrize("model", [
    "abliterate/abliterate-0.3-fast", "abliterate/abliterate-0.3-balanced",
    "abliterate/abliterate-0.3-clever",
])
@pytest.mark.parametrize("route", ["chat.completions", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_abliterate_freezes_prices_without_enabling_heartbeats(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch,
    model: str, route: str, stream: bool,
) -> None:
    store, _db, key = prod_store
    data = gateway._authorize_gateway_sync(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, model=model, estimated_input_tokens=100,
            max_output_tokens=100, stream=stream, route_type=route,
            provider={"only": ["abliterate"], "allow_fallbacks": False},
        ), INTERNAL,
    )["data"]
    assert data["candidate_cost_reporting"] is True
    assert data["stage_d"]["eligible"] is False
    assert data["cap_micro"] == data["estimated_cost_microdollars"]
    auth = store.get_gateway_authorization(data["authorization_id"])
    assert auth is not None
    snapshot = billing_pricing_snapshot(auth)
    assert snapshot is not None
    assert data["candidate_prices"] == snapshot["candidates"]
    expected = gateway._endpoint_cost_microdollars_from_document(
        snapshot, data["endpoint_id"],
        input_tokens=14, output_tokens=7,
    )
    refresh(data["endpoint_id"], monkeypatch)
    body = _settle_body(data).model_copy(update={"model": model, "route_type": route})
    settled = gateway._settle_gateway_authorization(body, success=True, settings=INTERNAL)["data"]
    assert settled["cost_microdollars"] == expected
    replay = gateway._settle_gateway_authorization(body, success=True, settings=INTERNAL)["data"]
    assert replay["already_settled"] is True
    assert replay["cost_microdollars"] == expected


@pytest.mark.parametrize("change", [
    {"provider": "other"}, {"model_id": "abliterate/abliterated-research-0.1"},
    {"candidate_model_ids": ["other/model"]}, {"usage_type": "BYOK"},
    {"custom_model_id": "wrapper"}, {"user_provided_model_id": "owner"},
    {"native_batch_eligible": True}, {"video_pricing_snapshot": "video"},
    {"additional_cost_reservation_microdollars": 1}, {"receipt_fee_basis_points": 1},
    {"app_markup_basis_points": 1}, {"custom_model_markup_basis_points": 1},
])
def test_estimated_snapshot_keeps_all_money_exclusions(
    fixed_catalog: str, change: dict[str, Any],
) -> None:
    from tests.test_snapshot_billing import eligible_auth
    auth = eligible_auth(fixed_catalog)
    auth.stage_d_reason = ESTIMATED_USAGE_SNAPSHOT_REASON
    auth.provider = "abliterate"
    auth.model_id = "abliterate/abliterate-0.3-fast"
    assert billing_pricing_snapshot(auth) is not None
    for field, value in change.items():
        setattr(auth, field, value)
    assert billing_pricing_snapshot(auth) is None


def test_frozen_prices_alone_never_admit_heartbeat(fixed_catalog: str) -> None:
    from tests.test_snapshot_billing import eligible_auth
    auth = eligible_auth(fixed_catalog)
    auth.stage_d_reason = "not_streaming"
    assert gateway._gateway_stage_d_payload(auth)["stage_d"]["eligible"] is False
