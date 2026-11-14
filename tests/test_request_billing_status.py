from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _request,
    _seed_typed_gateway_store,
)
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.scopes import SCOPE_INFERENCE
from trusted_router.storage import STORE
from trusted_router.types import UsageType

TRACE = "rlog_00112233445566778899aabbccddeeff"
OTHER_TRACE = "rlog_ffeeddccbbaa99887766554433221100"
PATH = f"/v1/requests/{TRACE}/billing"


def _authorization():
    user = STORE.ensure_user("billing-reader@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="reader", creator_user_id=user.id,
        scopes=[SCOPE_INFERENCE],
    )
    auth = STORE.create_gateway_authorization(
        workspace_id=workspace.id, key_hash=key.hash, model_id="test/model",
        provider="test", usage_type=UsageType.CREDITS, estimated_microdollars=100,
        gateway_request_id=TRACE, credit_reservation_id=None,
    )
    return auth, {"Authorization": f"Bearer {raw}"}, user


@pytest.mark.parametrize("settled,outcome,cost,status,reported", [
    (False, None, None, "pending", None),
    (True, None, None, "unknown", None),
    (True, "refunded", 0, "refunded", 0),
    (True, "settled", 129797, "settled", 129797),
    (True, "reaped_snapshot", 256625, "settled", 256625),
    (True, "reaped_snapshot", None, "unknown", None),
])
def test_billing_lookup_uses_durable_terminal_not_analytics(
    client: TestClient, settled, outcome, cost, status, reported,
):
    auth, headers, _ = _authorization()
    auth.settled, auth.finalization_outcome, auth.finalized_cost_microdollars = settled, outcome, cost
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["data"] == {
        "gateway_request_id": TRACE, "billing_status": status,
        "cost_microdollars": reported, "currency": "USD",
        "finalization_outcome": outcome, "generation_id": None,
    }
    assert auth.id not in response.text
    assert auth.key_hash not in response.text


def test_billing_lookup_is_workspace_and_delegated_key_scoped(client: TestClient):
    auth, headers, user = _authorization()
    other, _ = STORE.create_api_key(
        workspace_id=auth.workspace_id, name="other app", creator_user_id=user.id,
        scopes=[SCOPE_INFERENCE],
    )
    assert client.get(PATH, headers=headers).status_code == 200
    for denied in [{"Authorization": f"Bearer {other}"}, {"x-trustedrouter-user": "stranger@example.com"}]:
        result = client.get(PATH, headers=denied)
        assert result.status_code == 404
        assert auth.workspace_id not in result.text
    assert client.get(PATH).status_code == 401


def test_unknown_request_never_reports_zero_cost(client: TestClient):
    _, headers, _ = _authorization()
    response = client.get(PATH.replace(TRACE, OTHER_TRACE), headers=headers)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    assert "cost_microdollars" not in response.text
    assert client.get(PATH.replace(TRACE, "invalid"), headers=headers).status_code == 400


def test_authorize_trace_is_excluded_from_idempotency_fingerprint():
    base = {"model": "test", "idempotency_key": "same", "api_key_hash": "test-key"}
    assert GatewayAuthorizeRequest(**base, gateway_request_id=TRACE).model_dump() == GatewayAuthorizeRequest(**base, gateway_request_id=OTHER_TRACE).model_dump()


def test_typed_authorize_persists_trace_before_any_heartbeat_or_terminal():
    store, _, key = _seed_typed_gateway_store()
    body = _body(key.hash).model_copy(update={"gateway_request_id": TRACE})
    result = gateway._authorize_gateway_sync(_request(), body, Settings(environment="test"))
    auth = store.get_gateway_authorization_by_gateway_request_id(TRACE)
    assert auth is not None and auth.id == result["data"]["authorization_id"]
    assert not auth.settled and auth.started_at is None
    replay = gateway._authorize_gateway_sync(
        _request(), body.model_copy(update={"gateway_request_id": OTHER_TRACE}), Settings(environment="test"),
    )
    assert replay["data"]["authorization_id"] == auth.id
    assert store.get_gateway_authorization_by_gateway_request_id(OTHER_TRACE) is None


@pytest.mark.parametrize("success,outcome", [(False, "refunded"), (True, "reaped_snapshot")])
def test_recovery_without_generation_does_not_erase_authorize_trace(success, outcome):
    auth, _, _ = _authorization()
    auth.record_finalization(success=success, actual_microdollars=7, selected_usage_type=UsageType.CREDITS, generation=None, outcome=outcome)
    assert auth.gateway_request_id == TRACE


@pytest.mark.parametrize("refund_first", [False, True])
def test_typed_terminal_race_preserves_charge_and_trace(refund_first):
    store, _, key = _seed_typed_gateway_store()
    body = _body(key.hash).model_copy(update={"gateway_request_id": TRACE})
    response = gateway._authorize_gateway_sync(_request(), body, Settings(environment="test"))
    auth_id = response["data"]["authorization_id"]
    first = store.typed_finalize_gateway_authorization_result(
        auth_id, success=not refund_first, actual_microdollars=0 if refund_first else 40,
        selected_usage_type="Credits",
    )
    assert first.finalized
    second = store.typed_finalize_gateway_authorization_result(
        auth_id, success=refund_first, actual_microdollars=40 if refund_first else 0,
        selected_usage_type="Credits",
    )
    assert not second.finalized
    final = store.get_gateway_authorization_by_gateway_request_id(TRACE)
    assert final is not None and final.settled
    assert final.finalization_outcome == ("refunded" if refund_first else "settled")
    assert final.finalized_cost_microdollars == (0 if refund_first else 40)
