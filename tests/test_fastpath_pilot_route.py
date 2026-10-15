"""The fast path's pilot at the authorize and settle routes
(docs/design/fast-admission-pilot.md): a request in the cohort is held under
a lease and reserves nothing; its terminal goes to the front door and leaves
records; everything else, and every answer but `admitted`, is today's path."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastapi import Request

from tests.fakes.spanner import make_fake_store
from tests.test_fastpath_frontdoor import _Door, _transport
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, Workspace

WORKSPACE = "ws-pilot"
MODEL = "anthropic/claude-haiku-4.5"
ADMIT = {"Status": "admitted", "Envelope": "sealed-pilot", "EndOfLife": "2026-10-14T20:00:00Z"}
WON = {"Status": "won", "Kind": "settle", "Charge": 306, "Outcome": "", "Cost": 0, "CostKnown": False}


def _pilot(**overrides: Any) -> Settings:
    options: dict[str, Any] = {
        "environment": "test",
        "service_surface": "internal",
        "fastpath_pilot_enabled": True,
        "fastpath_pilot_workspaces": WORKSPACE,
        "settle_outbox_enabled": True,
    }
    options.update(overrides)
    return Settings(**options)


def _seed() -> tuple[Any, Any, Any]:
    store, database = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
        analytics_outbox_enabled=True,
    )
    workspace = Workspace(id=WORKSPACE, name="Pilot", owner_user_id="user-pilot")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id, name="pilot-key", creator_user_id=workspace.owner_user_id,
    )
    configure_store(store)
    return store, database, key


def _credit(database: Any) -> dict[str, Any]:
    return dict(database.typed[CREDIT_BALANCE_TABLE][(WORKSPACE, 0)])


def _request(*, boot: bool = True) -> Request:
    headers = [(b"x-tr-boot-auth", b"kid=boot-pilot,sig=x")] if boot else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def _body(key_hash: str, **overrides: Any) -> GatewayAuthorizeRequest:
    options: dict[str, Any] = {
        "api_key_hash": key_hash,
        "model": MODEL,
        "estimated_input_tokens": 100,
        "max_output_tokens": 100,
        "stream": False,
        "route_type": "chat.completions",
    }
    options.update(overrides)
    return GatewayAuthorizeRequest(**options)


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> _Door:
    """One front door, answering admitted and won, standing in for the fleet."""
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    door = _Door({"/v1/authorize": ADMIT, "/v1/settle": dict(WON), "/v1/refund": {"Status": "won", "Kind": "refund", "Charge": 0}})
    client = fd.FrontDoorClient(lambda _budget: ("10.0.0.1:8080",), timeout_seconds=1.5, transport=_transport({"10.0.0.1:8080": door}))
    monkeypatch.setattr(gateway, "_fastpath_client", client)
    return door


def _settle_body(authorized: dict[str, Any], *, success: bool = True) -> GatewaySettleRequest:
    return GatewaySettleRequest(
        authorization_id=authorized["authorization_id"],
        actual_input_tokens=90,
        actual_output_tokens=40,
        request_id="req-pilot",
        finish_reason="stop",
        status="success" if success else "error",
        streamed=False,
        elapsed_seconds=1.0,
        selected_model=MODEL,
        selected_endpoint=authorized["endpoint_id"],
        error_status=None if success else 502,
        error_type=None if success else "provider_error",
    )


def test_a_request_in_the_cohort_is_held_under_a_lease_and_reserves_nothing(door: _Door) -> None:
    store, database, key = _seed()
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert authorization.fastpath_envelope == "sealed-pilot"
    assert authorization.fastpath_end_of_life == "2026-10-14T20:00:00Z"
    # The scope row holds nothing: today's idempotency scope, no hold.
    assert authorization.credit_reservation_id is not None
    scope_row = database.reservations[authorization.credit_reservation_id]
    assert scope_row["credit_reserved_micro"] == 0 and scope_row["key_reserved_micro"] == 0
    assert scope_row["authorization_id"] == authorization.id
    assert _credit(database)["reserved"] == 0
    path, sent = door.seen[0]
    assert path == "/v1/authorize"
    assert sent["Workspace"] == WORKSPACE and sent["Request"] == authorization.id
    assert sent["Estimate"] == authorization.estimated_microdollars > 0
    assert sent["Stream"] is False
    assert sent["Boot"] == base64.b64encode(b"boot-pilot").decode()


def test_a_leased_authorization_replays_by_its_idempotency_key_and_gives_the_second_hold_back(door: _Door) -> None:
    """The enclave retries an authorize with the same key after a lost
    answer, and a client may present one: the stored authorization answers,
    no second row is written, and the hold the retry took is refunded."""
    store, database, key = _seed()
    for presented in ("tr-gateway-" + "ab" * 16, "client-order-42"):
        body = _body(key.hash, idempotency_key=presented)
        first = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
        seen_before = len(door.seen)
        again = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
        assert again["authorization_id"] == first["authorization_id"]
        assert again.get("idempotent_replay") is True
        authorization = store.get_gateway_authorization(first["authorization_id"])
        assert authorization is not None and authorization.fastpath_envelope == "sealed-pilot"
        assert authorization.idempotency_key == presented
        # The retry was admitted by the front door, then its hold refunded.
        assert [path for path, _sent in door.seen[seen_before:]] == ["/v1/authorize", "/v1/refund"]
        assert _credit(database)["reserved"] == 0
    # A different body under a used key is a mismatch, as today, and the
    # hold the mismatching attempt took is given back.
    seen_before = len(door.seen)
    with pytest.raises(Exception, match="") as caught:
        gateway._authorize_gateway_sync(_request(), _body(key.hash, idempotency_key="client-order-42", max_output_tokens=7), _pilot())
    assert getattr(caught.value, "status_code", None) == 409
    assert [path for path, _sent in door.seen[seen_before:]] == ["/v1/authorize", "/v1/refund"]


def test_one_idempotency_scope_across_both_admission_paths(door: _Door) -> None:
    """A key first used on today's path (the front door busy) then admitted,
    and a key first admitted then taking today's path: one authorization
    either way."""
    store, database, key = _seed()
    # Today's path first: the front door is busy.
    door.answers["/v1/authorize"] = {"Status": "busy"}
    body = _body(key.hash, idempotency_key="reserved-first")
    first = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
    assert store.get_gateway_authorization(first["authorization_id"]).fastpath_envelope is None
    door.answers["/v1/authorize"] = dict(ADMIT)
    again = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
    assert again["authorization_id"] == first["authorization_id"] and again.get("idempotent_replay") is True
    assert [path for path, _sent in door.seen[-2:]] == ["/v1/authorize", "/v1/refund"]
    # The pilot first: then the front door is busy on the retry.
    body = _body(key.hash, idempotency_key="leased-first")
    leased = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
    assert store.get_gateway_authorization(leased["authorization_id"]).fastpath_envelope == "sealed-pilot"
    door.answers["/v1/authorize"] = {"Status": "busy"}
    reserved_before = _credit(database)["reserved"]
    again = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
    assert again["authorization_id"] == leased["authorization_id"] and again.get("idempotent_replay") is True
    assert _credit(database)["reserved"] == reserved_before, "no second hold on today's path"
    # And a differing body under the leased key on today's path is 409.
    with pytest.raises(Exception, match="") as caught:
        gateway._authorize_gateway_sync(_request(), _body(key.hash, idempotency_key="leased-first", max_output_tokens=7), _pilot())
    assert getattr(caught.value, "status_code", None) == 409


def test_turning_the_pilot_off_keeps_replay_protection_for_leased_keys(door: _Door) -> None:
    store, database, key = _seed()
    body = _body(key.hash, idempotency_key="leased-then-off")
    leased = gateway._authorize_gateway_sync(_request(), body, _pilot())["data"]
    reserved_before = _credit(database)["reserved"]
    again = gateway._authorize_gateway_sync(_request(), body, _pilot(fastpath_pilot_enabled=False))["data"]
    assert again["authorization_id"] == leased["authorization_id"] and again.get("idempotent_replay") is True
    assert _credit(database)["reserved"] == reserved_before
    with pytest.raises(Exception, match="") as caught:
        gateway._authorize_gateway_sync(
            _request(), _body(key.hash, idempotency_key="leased-then-off", max_output_tokens=7), _pilot(fastpath_pilot_enabled=False),
        )
    assert getattr(caught.value, "status_code", None) == 409
