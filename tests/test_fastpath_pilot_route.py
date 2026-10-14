"""The fast path's pilot at the authorize and settle routes
(docs/design/fast-admission-pilot.md): a request in the cohort is held under
a lease and reserves nothing; its terminal goes to the front door and leaves
records; everything else, and every answer but `admitted`, is today's path."""

from __future__ import annotations

import base64
import json
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
WON = {"Status": "won", "Kind": "settle", "Charge": 0, "Outcome": "", "Cost": 0, "CostKnown": False}


def _pilot(**overrides: Any) -> Settings:
    options: dict[str, Any] = {
        "environment": "test",
        "service_surface": "internal",
        "fastpath_pilot_enabled": True,
        "fastpath_pilot_workspaces": WORKSPACE,
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
    door = _Door({"/v1/authorize": ADMIT, "/v1/settle": WON, "/v1/refund": {"Status": "won", "Kind": "refund"}})
    client = fd.FrontDoorClient(lambda: ("10.0.0.1:8080",), timeout_seconds=1.5, transport=_transport({"10.0.0.1:8080": door}))
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
    assert authorization.credit_reservation_id is None
    assert data["credit_reservation_id"] is None
    assert _credit(database)["reserved"] == 0
    assert database.reservations == {}
    path, sent = door.seen[0]
    assert path == "/v1/authorize"
    assert sent["Workspace"] == WORKSPACE and sent["Request"] == authorization.id
    assert sent["Estimate"] == authorization.estimated_microdollars > 0
    assert sent["Stream"] is False
    assert sent["Boot"] == base64.b64encode(b"boot-pilot").decode()


@pytest.mark.parametrize(
    ("settings", "body_overrides", "request_boot"),
    [
        (_pilot(fastpath_pilot_enabled=False), {}, True),
        (_pilot(fastpath_pilot_workspaces="ws-other"), {}, True),
        (_pilot(fastpath_pilot_regions="europe-west4"), {}, True),
        (_pilot(), {"stream": True}, True),
        (_pilot(), {"idempotency_key": "idem-1"}, True),
        (_pilot(), {}, False),
    ],
    ids=["disabled", "workspace", "region", "stream", "idempotency", "no_boot"],
)
def test_a_request_outside_the_cohort_reserves_as_today(
    door: _Door, settings: Settings, body_overrides: dict[str, Any], request_boot: bool,
) -> None:
    store, database, key = _seed()
    data = gateway._authorize_gateway_sync(_request(boot=request_boot), _body(key.hash, **body_overrides), settings)["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert authorization.fastpath_envelope is None
    assert authorization.credit_reservation_id is not None
    assert _credit(database)["reserved"] == authorization.estimated_microdollars > 0
    assert door.seen == []


@pytest.mark.parametrize("status", ["off", "busy", "invalid", "failed"])
def test_every_front_door_answer_but_admitted_reserves_as_today(door: _Door, status: str) -> None:
    store, database, key = _seed()
    door.answers["/v1/authorize"] = {"Status": status}
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert _credit(database)["reserved"] == authorization.estimated_microdollars > 0
    assert len(door.seen) == 1


def test_a_front_door_that_cannot_be_reached_reserves_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    client = fd.FrontDoorClient(lambda: ("10.0.0.9:8080",), timeout_seconds=1.5, transport=_transport({}, down={"10.0.0.9:8080"}))
    monkeypatch.setattr(gateway, "_fastpath_client", client)
    store, database, key = _seed()
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert _credit(database)["reserved"] > 0


def test_the_settle_of_a_leased_request_goes_to_the_front_door_and_leaves_records(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    credit_before = _credit(database)

    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]

    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert authorization.finalization_outcome == "settled"
    assert authorization.finalized_cost_microdollars is not None and authorization.finalized_cost_microdollars > 0
    path, sent = door.seen[1]
    assert path == "/v1/settle"
    assert sent["Envelope"] == "sealed-pilot"
    assert sent["Charge"] == authorization.finalized_cost_microdollars
    assert json.loads(sent["Money"] if isinstance(sent["Money"], str) else json.dumps(sent["Money"])) == {
        "charge": authorization.finalized_cost_microdollars,
        "estimate": authorization.estimated_microdollars,
    }
    full = json.loads(base64.b64decode(sent["Full"]))
    assert full["boot"] == base64.b64encode(b"boot-pilot").decode()
    assert full["authorization_id"] == authorization.id and full["kind"] == "settle"
    # No money moved here: the lease booked the usage.
    assert _credit(database) == credit_before
    assert database.reservations == {}
    activity = [json.loads(row["payload"]) for row in database.operational_analytics_outbox]
    assert [(row["request_id"], row["workspace_id"]) for row in activity] == [("req-pilot", WORKSPACE)]
    assert data["settled"] is True


def test_a_second_settle_of_a_leased_request_is_a_replay(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    first = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    again = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert again["authorization_id"] == first["authorization_id"] and again["settled"] is True
    assert len([path for path, _sent in door.seen if path == "/v1/settle"]) == 1
    assert len(database.operational_analytics_outbox) == 1


def test_the_refund_of_a_leased_request_goes_to_the_front_door_and_records_a_refund(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    credit_before = _credit(database)
    gateway._settle_gateway_authorization(_settle_body(authorized, success=False), success=False, settings=_pilot())
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert authorization.finalization_outcome == "refunded"
    path, sent = door.seen[1]
    assert path == "/v1/refund" and sent["Envelope"] == "sealed-pilot"
    assert _credit(database) == credit_before
    assert database.operational_analytics_outbox == []


def test_a_front_door_that_cannot_take_the_terminal_answers_503_and_keeps_the_request_open(
    door: _Door, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    down = fd.FrontDoorClient(lambda: ("10.0.0.9:8080",), timeout_seconds=1.5, transport=_transport({}, down={"10.0.0.9:8080"}))
    monkeypatch.setattr(gateway, "_fastpath_client", down)
    with pytest.raises(Exception) as caught:
        gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    assert getattr(caught.value, "status_code", None) == 503
    assert caught.value.headers.get("Retry-After") == "1"
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and not authorization.settled


def test_a_lease_closed_before_the_terminal_records_what_the_fast_path_booked(door: _Door) -> None:
    store, _database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 5, "CostKnown": True}
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert authorization.finalized_cost_microdollars == 5
