"""Seed rollout recovery never invokes authorization or reserves a new hold."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from trusted_router import storage
from trusted_router.routes.internal import gateway, video_jobs
from trusted_router.schemas import GatewayAuthorizeRequest


def _fingerprint_body(body):
    material = body.model_dump(exclude_none=True)
    for field in ("additional_cost_reservation_microdollars", "invocation_nonce"):
        material.pop(field, None)
    if not body.inference_receipt:
        material.pop("inference_receipt", None)
    if body.tags is None:
        material.pop("tags", None)
    return material


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("case", [
    "replay", "miss", "fingerprint", "provider", "model", "scope",
    "disabled", "expired", "missing_key", "wrong_workspace", "wrong_key",
    "wrong_idempotency", "reservation", "nonce", "wrong_route",
])
def test_read_only_replay(monkeypatch, typed, case):
    body = GatewayAuthorizeRequest(
        api_key_lookup_hash="a" * 64, model="minimax/hailuo-3",
        route_type="videos", idempotency_key="original-key",
        request_fingerprint="b" * 64, max_tokens=1, max_output_tokens=1,
        estimated_input_tokens=1, region="azure-westeurope",
        provider={"only": ["minimax"]},
    )
    old_body = body.model_copy(update={"region": "gcp-us-central1", "additional_cost_reservation_microdollars": 500000})
    authorization = SimpleNamespace(
        id="old", workspace_id="ws", key_hash="hash", model_id=body.model,
        idempotency_key=body.idempotency_key, region=old_body.region,
        idempotency_fingerprint=gateway._gateway_authorize_fingerprint(
            workspace_id="ws", key_hash="hash", body=_fingerprint_body(old_body),
            idempotency_key=body.idempotency_key,
        ),
    )
    key = SimpleNamespace(workspace_id="ws", hash="hash", disabled=False,
                          expires_at=None, scopes=[])
    expected = None
    if case == "fingerprint":
        body = body.model_copy(update={"request_fingerprint": "c" * 64})
        expected = 409
    elif case == "provider":
        body = body.model_copy(update={"provider": {"only": ["venice"]}})
        expected = 409
    elif case == "model":
        body = body.model_copy(update={"model": "minimax/h3-max"})
        expected = 409
    elif case == "scope":
        key.scopes = ["billing:read"]
        expected = 403
    elif case == "disabled":
        key.disabled = True
        expected = 401
    elif case == "expired":
        key.expires_at = "2000-01-01T00:00:00+00:00"
        expected = 401
    elif case == "missing_key":
        key = None
        expected = 401
    elif case == "wrong_workspace":
        authorization.workspace_id = "other"
        expected = 409
    elif case == "wrong_key":
        authorization.key_hash = "other"
        expected = 409
    elif case == "wrong_idempotency":
        authorization.idempotency_key = "other"
        expected = 409
    elif case == "reservation":
        body = body.model_copy(update={"additional_cost_reservation_microdollars": 100})
        expected = 400
    elif case == "nonce":
        body = body.model_copy(update={"invocation_nonce": "new-dispatch"})
        expected = 400
    elif case == "wrong_route":
        body = body.model_copy(update={"route_type": "images"})
        expected = 400
    store = Mock(spec=["get_workspace", "get_gateway_authorization_by_idempotency_key"])
    store.get_workspace.return_value = SimpleNamespace(id="ws")
    lookup = store.get_gateway_authorization_by_idempotency_key
    typed_store = Mock(spec=["get_typed_authorization_by_idempotency"])
    if typed:
        lookup = typed_store.get_typed_authorization_by_idempotency
    lookup.return_value = None if case == "miss" else authorization
    monkeypatch.setattr(video_jobs, "STORE", store)
    monkeypatch.setattr(storage, "typed_billing_store", lambda _: typed_store if typed else None)
    monkeypatch.setattr(video_jobs, "require_internal_gateway", lambda *_: None)
    resolve = Mock(return_value=key)
    monkeypatch.setattr(gateway, "_api_key_for_gateway_lookup", resolve)
    if expected:
        with pytest.raises(HTTPException) as exc:
            video_jobs._replay_lookup(SimpleNamespace(), body, SimpleNamespace())
        assert exc.value.status_code == expected
    else:
        data = video_jobs._replay_lookup(SimpleNamespace(), body, SimpleNamespace())["data"]
        assert data == ({"found": False} if case == "miss" else {"found": True, "authorization": {
            "authorization_id": "old", "workspace_id": "ws", "api_key_hash": "hash",
            "model": "minimax/hailuo-3", "idempotent_replay": True,
        }})
        lookup.assert_called_once_with("ws", "hash", "original-key")
    if expected not in (400,):
        resolve.assert_called_once_with(api_key_hash=None, api_key_lookup_hash="a" * 64)
