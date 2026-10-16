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
from trusted_router.storage_models import CreditAccount, SettleOutboxRow, Workspace

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


@pytest.mark.parametrize(
    ("settings", "body_overrides", "request_boot"),
    [
        (_pilot(fastpath_pilot_enabled=False), {}, True),
        (_pilot(fastpath_pilot_workspaces="ws-other"), {}, True),
        (_pilot(fastpath_pilot_regions="europe-west4"), {}, True),
        (_pilot(), {"stream": True}, True),
        (_pilot(), {}, False),
    ],
    ids=["disabled", "workspace", "region", "stream", "no_boot"],
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
    client = fd.FrontDoorClient(lambda _budget: ("10.0.0.9:8080",), timeout_seconds=1.5, transport=_transport({}, down={"10.0.0.9:8080"}))
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
    assert json.loads(base64.b64decode(sent["Money"])) == {
        "charge": authorization.finalized_cost_microdollars,
        "estimate": authorization.estimated_microdollars,
    }
    full = json.loads(base64.b64decode(sent["Full"]))
    assert full["boot"] == base64.b64encode(b"boot-pilot").decode()
    assert full["authorization_id"] == authorization.id and full["kind"] == "settle"
    # No money moved here: the lease booked the usage.
    assert _credit(database) == credit_before
    # The scope row held nothing and is claimed with the records.
    (scope_row,) = database.reservations.values()
    assert scope_row["settled"] and scope_row["credit_reserved_micro"] == 0 and scope_row["key_reserved_micro"] == 0
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
    # The provider-error benchmark rides with the refund, as today.
    benchmarks = [json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row for row in database.analytics_outbox]
    assert len(benchmarks) == 1, benchmarks
    intents = [row for row in database.settle_outbox.values() if row["authorization_id"] == authorization.id]
    assert len(intents) == 1 and intents[0]["intent_kind"] == "refund" and intents[0]["auto_refill_workspace_id"] is None


def test_a_front_door_that_cannot_take_the_terminal_leaves_it_durable_and_pending(
    door: _Door, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    down = fd.FrontDoorClient(lambda _budget: ("10.0.0.9:8080",), timeout_seconds=1.5, transport=_transport({}, down={"10.0.0.9:8080"}))
    monkeypatch.setattr(gateway, "_fastpath_client", down)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable" and data["settled"] is False
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and not authorization.settled
    assert _pending_intents(database, authorization.id)[0]["status"] == "pending"


def test_the_records_say_what_the_owner_decided_not_what_the_route_priced(door: _Door) -> None:
    store, _database, key = _seed()  # noqa: F841 - the database is read below
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "won", "Kind": "settle", "Charge": 7}
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert authorization.finalized_cost_microdollars == 7
    activity = [json.loads(row["payload"]) for row in _database.operational_analytics_outbox]
    assert [row["total_cost_microdollars"] for row in activity] == [7]
    intents = [row for row in _database.settle_outbox.values() if row["authorization_id"] == authorization.id]
    # The intent froze the priced charge before the front door answered; the
    # authorization and the records carry what the owner decided.
    assert len(intents) == 1 and intents[0]["status"] == "done" and intents[0]["actual_cost_micro"] == 306
    assert intents[0]["auto_refill_workspace_id"] == WORKSPACE
    assert _database.gateway_authorizations[authorization.id]["terminal_at"] is not None


def test_a_lease_closed_before_the_terminal_records_the_disposition(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 5, "CostKnown": True}
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert authorization.finalization_outcome == "settled" and authorization.finalized_cost_microdollars == 5
    # A settle answered with a refunded disposition records a refund, no generation.
    second = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "settled", "Outcome": "refunded", "Cost": 0, "CostKnown": True}
    activity_before = len(database.operational_analytics_outbox)
    gateway._settle_gateway_authorization(_settle_body(second), success=True, settings=_pilot())
    refunded = store.get_gateway_authorization(second["authorization_id"])
    assert refunded is not None and refunded.settled and refunded.finalization_outcome == "refunded"
    assert len(database.operational_analytics_outbox) == activity_before
    # A refund answered with a booking has no generation to write: pending.
    third = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/refund"] = {"Status": "settled", "Outcome": "settled", "Cost": 306, "CostKnown": True}
    data = gateway._settle_gateway_authorization(_settle_body(third, success=False), success=False, settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable"
    open_row = store.get_gateway_authorization(third["authorization_id"])
    assert open_row is not None and not open_row.settled


def test_a_terminal_that_lost_to_another_kind_is_pending_not_recorded(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/refund"] = {"Status": "won", "Kind": "settle", "Charge": 306}
    data = gateway._settle_gateway_authorization(_settle_body(authorized, success=False), success=False, settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable"
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and not authorization.settled
    assert _pending_intents(database, authorization.id)[0]["intent_kind"] == "refund"


def _pending_intents(database: Any, authorization_id: str) -> list[dict[str, Any]]:
    return [row for row in database.settle_outbox.values() if row["authorization_id"] == authorization_id]


@pytest.mark.parametrize("status", ["recorded", "invalid", "failed", "something_new"])
def test_a_terminal_the_fast_path_has_not_decided_is_durable_and_pending(door: _Door, status: str) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": status}
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert data["settled"] is False and data["disposition"] == "intent_durable"
    assert data["finalization_outcome"] == "pending" and "generation_id" not in data
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and not authorization.settled
    intents = _pending_intents(database, authorization.id)
    assert len(intents) == 1 and intents[0]["status"] == "pending" and intents[0]["intent_kind"] == "settle"
    assert database.operational_analytics_outbox == []


def test_the_intent_is_durable_before_the_front_door_is_asked(door: _Door) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    seen_pending: list[bool] = []
    original = door.answers["/v1/settle"]

    class _Peek(dict):  # the door's answer is read when the request arrives
        def __getitem__(self, path: str) -> Any:
            if path == "/v1/settle":
                seen_pending.append(bool(_pending_intents(database, authorized["authorization_id"])))
            return super().__getitem__(path)

    door.answers = _Peek({"/v1/settle": original, "/v1/authorize": ADMIT})
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    assert seen_pending == [True]
    row = _pending_intents(database, authorized["authorization_id"])[0]
    assert row["status"] == "done"
    settled_row = store.get_gateway_authorization(authorized["authorization_id"])
    assert settled_row is not None and settled_row.settled


def test_a_reap_that_lands_before_the_intent_answers_the_refund_and_asks_no_front_door(
    door: _Door, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settle read the leased authorization unsettled; the reaper then
    finalized it, expired and holding nothing, before the intent was
    enqueued. The settle answers the stored refund and forwards no charge
    the records would never show; the drain marks its intent done."""
    import datetime as dt

    from trusted_router.services import settle_outbox_drain

    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    now = dt.datetime.now(dt.UTC)
    database.reservations[authorization.credit_reservation_id]["expires_at"] = now - dt.timedelta(seconds=1)
    outbox = gateway.spanner_settle_outbox()
    reaped: list[int] = []

    class _ReapFirst:
        def __getattr__(self, name: str) -> Any:
            return getattr(outbox, name)

        def enqueue(self, *args: Any, **kwargs: Any) -> Any:
            reaped.append(store.reap_expired_reservations_result(now=now, limit=10).count)
            return outbox.enqueue(*args, **kwargs)

    monkeypatch.setattr(gateway, "spanner_settle_outbox", lambda: _ReapFirst())
    doors_before = len(door.seen)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert reaped == [1], "the reaper finalized the leased authorization first"
    assert [path for path, _sent in door.seen[doors_before:]] == [], "no charge is forwarded"
    assert data["already_settled"] is True and data["finalization_outcome"] == "refunded"
    reaped_row = store.get_gateway_authorization(authorization.id)
    assert reaped_row is not None and reaped_row.finalization_outcome == "refunded"
    assert _credit(database)["reserved"] == 0 and _credit(database)["total_usage"] == 0
    monkeypatch.undo()
    row = _pending_intents(database, authorization.id)[0]
    row["next_attempt_at"] = "2020-01-01T00:00:00Z"
    settle_outbox_drain.drain_settle_outbox(10)
    assert len(door.seen) == doors_before
    assert _pending_intents(database, authorization.id)[0]["status"] == "done"


def _expiry(row: dict[str, Any]) -> Any:
    import datetime as dt

    value = row["expires_at"]
    return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def test_an_abandoned_leased_authorization_is_reaped_on_todays_schedule(door: _Door) -> None:
    """A leased authorization that never sees a terminal expires as today's
    does: the reaper finalizes it released, holding nothing, so no unsettled
    reservation outlives it (the resharders count those)."""
    import datetime as dt

    store, database, key = _seed()
    leased = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/authorize"] = {"Status": "busy"}
    control = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    leased_auth = store.get_gateway_authorization(leased["authorization_id"])
    control_auth = store.get_gateway_authorization(control["authorization_id"])
    assert leased_auth is not None and leased_auth.fastpath_envelope == "sealed-pilot"
    assert control_auth is not None and control_auth.fastpath_envelope is None
    leased_expiry = _expiry(database.reservations[leased_auth.credit_reservation_id])
    control_expiry = _expiry(database.reservations[control_auth.credit_reservation_id])
    assert abs(leased_expiry - control_expiry) < dt.timedelta(minutes=1)
    after = max(leased_expiry, control_expiry) + dt.timedelta(seconds=1)
    assert store.reap_expired_reservations_result(now=after, limit=10).count == 2
    for authorization_id in (leased_auth.id, control_auth.id):
        reaped = store.get_gateway_authorization(authorization_id)
        assert reaped is not None and reaped.settled and reaped.finalization_outcome == "refunded"
    assert not [row for row in database.reservations.values() if not row["settled"]]
    assert _credit(database)["reserved"] == 0 and _credit(database)["total_usage"] == 0


def test_a_pending_intent_keeps_the_reaper_off_a_leased_authorization(door: _Door) -> None:
    """Enqueued first, the intent fences the reaper: an expired leased
    authorization with a pending intent is left for the terminal."""
    import datetime as dt

    store, database, key = _seed()
    door.answers["/v1/settle"] = {"Status": "recorded"}
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable"
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and not authorization.settled
    now = dt.datetime.now(dt.UTC)
    database.reservations[authorization.credit_reservation_id]["expires_at"] = now - dt.timedelta(seconds=1)
    assert store.reap_expired_reservations_result(now=now, limit=10).count == 0
    still = store.get_gateway_authorization(authorization.id)
    assert still is not None and not still.settled


def test_a_record_failure_after_a_decided_terminal_is_left_to_the_drain(door: _Door, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router import storage_gcp

    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    original = storage_gcp.SpannerStore.typed_finalize_leased_result
    calls = {"n": 0}

    def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeadlineExceeded("spanner")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(storage_gcp.SpannerStore, "typed_finalize_leased_result", flaky)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable"
    row = _pending_intents(database, authorized["authorization_id"])[0]
    assert row["status"] == "pending"
    # The drain finishes it with the records, and the row is done.
    from trusted_router.services import settle_outbox_drain

    row["next_attempt_at"] = "2020-01-01T00:00:00Z"
    settle_outbox_drain.drain_settle_outbox(10)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled
    assert _pending_intents(database, authorization.id)[0]["status"] == "done"
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is not None


def test_the_drain_marks_a_losing_intent_done_without_asking_the_front_door(door: _Door) -> None:
    from trusted_router.services import settle_outbox_drain

    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    # A sibling refund intent, pending, for the same authorization (the
    # done settle row has no body any more; a pending row carries one).
    database.settle_outbox[(authorized["authorization_id"], "refund")] = dict(
        database.settle_outbox[(authorized["authorization_id"], "settle")],
        intent_kind="refund", status="pending", actual_cost_micro=0, next_attempt_at="2020-01-01T00:00:00Z",
        lease_owner=None, leased_until=None, attempts=0, terminal_at=None,
        settle_body=_settle_body(authorized, success=False).model_dump_json(exclude_none=True),
        selected_usage_type="Credits", settle_origin="typed",
    )
    doors_before = len(door.seen)
    settle_outbox_drain.drain_settle_outbox(10)
    assert len(door.seen) == doors_before
    assert database.settle_outbox[(authorized["authorization_id"], "refund")]["status"] == "done"
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.finalization_outcome == "settled"


def test_the_drain_parks_a_leased_intent_when_the_store_is_away(door: _Door, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router import storage_gcp
    from trusted_router.services import settle_outbox_drain

    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "recorded"}
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    door.answers["/v1/settle"] = dict(WON)

    def away(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise DeadlineExceeded("spanner")

    monkeypatch.setattr(storage_gcp.SpannerStore, "typed_finalize_leased_result", away)
    row = _pending_intents(database, authorized["authorization_id"])[0]
    attempts_before = row.get("attempts") or 0
    row["next_attempt_at"] = "2020-01-01T00:00:00Z"
    settle_outbox_drain.drain_settle_outbox(10)
    row = _pending_intents(database, authorized["authorization_id"])[0]
    assert row["status"] == "pending" and (row.get("attempts") or 0) == attempts_before
    assert not (store.get_gateway_authorization(authorized["authorization_id"]) or authorization_absent()).settled


def authorization_absent() -> Any:
    raise AssertionError("the authorization row is gone")


def test_the_drain_finishes_a_leased_intent_the_front_door_decides_later(door: _Door) -> None:
    from trusted_router.services import settle_outbox_drain

    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    door.answers["/v1/settle"] = {"Status": "recorded"}
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    row = _pending_intents(database, authorized["authorization_id"])[0]
    assert row["status"] == "pending"
    row["next_attempt_at"] = "2020-01-01T00:00:00Z"  # due now
    # Still undecided: the row stays pending for a later pass.
    settle_outbox_drain.drain_settle_outbox(10)
    assert _pending_intents(database, authorized["authorization_id"])[0]["status"] == "pending"
    open_row = store.get_gateway_authorization(authorized["authorization_id"])
    assert open_row is not None and not open_row.settled
    # Decided at last: the records are written and the row is done.
    door.answers["/v1/settle"] = {"Status": "won", "Kind": "settle", "Charge": 306}
    row = _pending_intents(database, authorized["authorization_id"])[0]
    row["next_attempt_at"] = "2020-01-01T00:00:00Z"
    row["lease_owner"] = None
    row["leased_until"] = None
    settle_outbox_drain.drain_settle_outbox(10)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.settled and authorization.finalized_cost_microdollars == 306
    assert _pending_intents(database, authorized["authorization_id"])[0]["status"] == "done"
    assert len(database.operational_analytics_outbox) == 1
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is not None
    assert _credit(database)["reserved"] == 0


def test_a_failed_insert_after_admission_gives_the_hold_back(door: _Door, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router import storage_gcp

    store, _database, key = _seed()

    def failing(*_args: Any, **_kwargs: Any) -> None:
        raise DeadlineExceeded("spanner")

    monkeypatch.setattr(storage_gcp.SpannerStore, "_authorize_gateway_leased", failing)
    with pytest.raises(DeadlineExceeded):
        gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())
    assert [path for path, _sent in door.seen] == ["/v1/authorize", "/v1/refund"]
    assert door.seen[1][1]["Envelope"] == "sealed-pilot"


def test_a_byok_terminal_charges_the_lease_nothing(door: _Door) -> None:
    """A key's own provider paid: today's path books no credit usage for a
    BYOK terminal, and the lease is told nothing either."""
    store, _database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    intent = SettleOutboxRow(
        authorization_id=authorization.id, intent_kind="settle", settle_origin="typed", actual_cost_micro=306,
        selected_usage_type="BYOK",
    )
    gateway._settle_leased_authorization(
        authorization, settings=_pilot(), success=True, actual_cost=306, selected_usage_type="BYOK",
        generation=None, refund_benchmark=None, settle_intent=intent, typed_store=gateway.typed_billing_store(store),
    )
    path, sent = door.seen[1]
    assert path == "/v1/settle" and sent["Charge"] == 0
    assert json.loads(base64.b64decode(sent["Money"]))["charge"] == 0


def test_a_route_with_a_byok_candidate_stays_on_todays_path(door: _Door, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, ModelEndpoint

    store, database, key = _seed()
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == MODEL:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    for usage_type, suffix in (("Credits", "prepaid"), ("BYOK", "byok")):
        endpoint = ModelEndpoint(
            id=f"{MODEL}@anthropic/{suffix}", model_id=MODEL, provider="anthropic", usage_type=usage_type,
            upstream_id="claude-haiku-4-5-20251001", supported_parameters=("max_tokens",),
            prompt_price_microdollars_per_million_tokens=1_000_000,
            completion_price_microdollars_per_million_tokens=5_000_000,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    store.upsert_byok_provider(workspace_id=WORKSPACE, provider="anthropic", secret_ref="ref-1", key_hint=None)  # noqa: S106 - a reference, not a secret
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert {e.rsplit("@", 1)[-1] for e in authorization.candidate_endpoint_ids} >= {"anthropic/byok"}
    assert _credit(database)["reserved"] > 0
    assert door.seen == []


def test_a_control_plane_without_the_settle_outbox_stays_on_todays_path(door: _Door) -> None:
    store, database, key = _seed()
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot(settle_outbox_enabled=False))["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert _credit(database)["reserved"] > 0
    assert door.seen == []


def test_a_hold_not_given_back_after_a_failed_insert_is_an_error(
    door: _Door, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router import storage_gcp

    _store, _database, key = _seed()
    door.answers["/v1/refund"] = {"Status": "invalid"}

    def failing(*_args: Any, **_kwargs: Any) -> None:
        raise DeadlineExceeded("spanner")

    monkeypatch.setattr(storage_gcp.SpannerStore, "_authorize_gateway_leased", failing)
    with pytest.raises(DeadlineExceeded), caplog.at_level("ERROR", logger="trusted_router.routes.internal.gateway"):
        gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())
    assert any("NOT released" in record.getMessage() for record in caplog.records)


def test_a_key_with_an_alert_only_window_stays_on_todays_path(door: _Door) -> None:
    store, database, _key = _seed()
    _raw, key = store.api_keys.create(
        workspace_id=WORKSPACE, name="alert-window", creator_user_id="user-pilot",
        limit_daily_microdollars=1_000, budget_alert_only=True,
    )
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert _credit(database)["reserved"] > 0
    assert door.seen == []


def test_a_store_without_the_activity_outbox_stays_on_todays_path(door: _Door) -> None:
    store, database = make_fake_store(request_record_write_mode="typed", operational_analytics_outbox_enabled=False)
    workspace = Workspace(id=WORKSPACE, name="Pilot", owner_user_id="user-pilot")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": 50_000_000, "total_usage": 0, "reserved": 0,
        "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(workspace_id=workspace.id, name="k", creator_user_id="user-pilot")
    configure_store(store)
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert door.seen == []
