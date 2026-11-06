"""A leased stream's terminals and reaps (docs/design/fast-admission-streams.md,
increment 3, sections 6.3 and 6.4).

A terminal answered with the lease's reap of the stream's snapshot is
decided as Decision 70 decides it today: reaped_snapshot, the snapshot's
records at the lease's cost, nothing more booked. The reaper never decides a
heartbeated leased row and never refunds one: it reads the lease's
disposition through the front door and records what the lease decided, and
reads every other answer, an older front door's 404 included, as pending.

The front door here is a fake transport answering in the Go front door's own
JSON (fastpath/internal/frontdoor/answers.go: TerminalAnswer,
DispositionAnswer, whose Outcome, Cost and CostKnown are omitted when zero).
The repository has no harness that starts the Go service from Python.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from typing import Any

import httpx
import pytest

from tests.test_fastpath_pilot_route import (
    WORKSPACE,
    _body,
    _credit,
    _pilot,
    _request,
    _seed,
    _settle_body,
)
from trusted_router.routes.internal import gateway
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.services import fastpath_settle, settle_outbox_drain
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE
from trusted_router.storage_models import GatewayAuthorization, Generation
from trusted_router.types import UsageType

DOOR = "10.0.0.1:8080"
ADMIT = {"Status": "admitted", "Envelope": "sealed-pilot", "EndOfLife": "2026-10-14T20:00:00Z"}
REAP_WON = {"Status": "won", "Kind": "reap", "Charge": 77}
REAPED = {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 55, "CostKnown": True}
USAGE = {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "price_tier_input_tokens": 0, "reasoning_tokens": 0}


class _Fleet:
    """One front door: an answer (status code and JSON, or a timeout) per
    path, and every request it saw."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, Any]] = {
            "/v1/authorize": (200, ADMIT),
            "/v1/settle": (200, {"Status": "won", "Kind": "settle", "Charge": 306}),
            "/v1/refund": (200, {"Status": "won", "Kind": "refund", "Charge": 0}),
            "/v1/disposition": (200, {"Status": "pending"}),
        }
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.seen.append((request.url.path, json.loads(request.content)))
            status, body = self.answers[request.url.path]
            if status == 0:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handle)

    def paths(self) -> list[str]:
        return [path for path, _ in self.seen]


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> _Fleet:
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    fleet = _Fleet()
    client = fd.FrontDoorClient(lambda _budget: (DOOR,), timeout_seconds=1.5, clock=lambda: 0.0,
                                transport=fleet.transport())
    monkeypatch.setattr(gateway, "_fastpath_client", client)
    return fleet


def _leased_stream(store: Any, database: Any, key: Any) -> dict[str, Any]:
    """A leased authorization whose row a stream's heartbeats reached: a
    heartbeat sequence, the started marker, the selected endpoint and the
    delivered usage, as heartbeat_gateway_atomic writes them."""
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    row = database.gateway_authorizations[authorized["authorization_id"]]
    row["heartbeat_seq"] = 3
    row["started_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    row["selected_endpoint_id"] = authorized["endpoint_id"]
    row["delivered_usage"] = json.dumps(USAGE, sort_keys=True, separators=(",", ":"))
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None and authorization.started_at is not None
    assert authorization.fastpath_envelope == "sealed-pilot"
    return authorized


def _expire(database: Any, authorization: GatewayAuthorization) -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    database.reservations[authorization.credit_reservation_id]["expires_at"] = now - dt.timedelta(seconds=1)
    return now


def _generations(database: Any) -> list[dict[str, Any]]:
    return [json.loads(row["payload"]) for row in database.generation_records.values()]


def _assert_reaped(store: Any, database: Any, key: Any, authorization_id: str, cost: int) -> None:
    """The snapshot's records at the lease's cost, as today's reaper writes
    them, with no credit moved (the lease booked it) and the key's usage
    booked as the reaper's booking does."""
    authorization = store.get_gateway_authorization(authorization_id)
    assert authorization is not None and authorization.settled
    assert authorization.finalization_outcome == "reaped_snapshot"
    assert authorization.finalized_cost_microdollars == cost
    (generation,) = _generations(database)
    assert generation["settled_from"] == "heartbeat" and generation["usage_estimated"] is True
    assert generation["tokens_completion"] == USAGE["output_tokens"]
    assert authorization.finalized_generation_id == generation["id"]
    assert _credit(database)["reserved"] == 0 and _credit(database)["total_usage"] == 0
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["usage"] == cost


# -- forward_terminal ---------------------------------------------------------


class _Terminal:
    """A client answering every terminal with one TerminalAnswer."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    def _terminal(self) -> fd.Terminal:
        a = self.answer
        return fd.Terminal(status=a["Status"], kind=a.get("Kind", ""), charge=a.get("Charge", 0),
                           outcome=a.get("Outcome", ""), cost=a.get("Cost", 0), cost_known=a.get("CostKnown", False))

    def settle(self, **_kwargs: Any) -> fd.Terminal:
        return self._terminal()

    def refund(self, **_kwargs: Any) -> fd.Terminal:
        return self._terminal()


def _authorization(*, snapshot: bool = True) -> GatewayAuthorization:
    """A leased authorization; with `snapshot`, one a stream's heartbeat
    reached (the started marker and the delivered usage)."""
    return GatewayAuthorization(id="gwa-1", workspace_id=WORKSPACE, key_hash="k", model_id="m", provider="p",
                                usage_type=UsageType.CREDITS, estimated_microdollars=500,
                                fastpath_envelope="sealed", stage_d_boot_kid="boot",
                                started_at="2026-10-30T00:00:00Z" if snapshot else None,
                                delivered_usage=json.dumps(USAGE) if snapshot else None)


@pytest.mark.parametrize("answer", [REAP_WON, {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 0,
                                               "CostKnown": True}], ids=["won_reap", "auditor_zero_reap"])
def test_a_reap_of_a_hold_no_heartbeat_reached_keeps_todays_handling(answer: dict[str, Any]) -> None:
    """The auditor reaps an open hold with no heartbeat at its end of life,
    uncharged: no snapshot to record. A settle keeps its own generation at
    the lease's cost, outcome settled, as before; a refund stays undecided,
    as before; and a won reap, which no owner makes of such a hold, stays
    undecided."""
    plain = _authorization(snapshot=False)
    generation = Generation(id="gen-1", request_id="r", workspace_id=WORKSPACE, key_hash="k", model="m",
                            provider_name="p", app="a", tokens_prompt=1, tokens_completion=1,
                            total_cost_microdollars=300, usage_type="Credits", speed_tokens_per_second=1.0,
                            finish_reason="stop", status="success", streamed=False)
    if answer["Status"] == "won":
        for success in (True, False):
            with pytest.raises(fastpath_settle.Undecided):
                fastpath_settle.forward_terminal(_Terminal(answer), plain, success=success, charge=300,  # type: ignore[arg-type]
                                                 generation=generation)
        return
    decision = fastpath_settle.forward_terminal(_Terminal(answer), plain, success=True, charge=300,  # type: ignore[arg-type]
                                                generation=generation)
    assert decision.outcome is None and decision.success and decision.charge == 0
    assert decision.generation is not None and decision.generation.id == "gen-1"
    assert decision.generation.total_cost_microdollars == 0
    with pytest.raises(fastpath_settle.Undecided):
        fastpath_settle.forward_terminal(_Terminal(answer), plain, success=False, charge=0,  # type: ignore[arg-type]
                                         generation=None)


@pytest.mark.parametrize("success", [True, False], ids=["settle", "refund"])
@pytest.mark.parametrize("answer", [REAP_WON, REAPED], ids=["won_reap", "settled_reaped_snapshot"])
def test_a_terminal_after_a_reap_is_decided_as_the_snapshot(success: bool, answer: dict[str, Any]) -> None:
    decision = fastpath_settle.forward_terminal(
        _Terminal(answer), _authorization(), success=success, charge=300, generation=None,  # type: ignore[arg-type]
    )
    cost = answer.get("Charge") if answer["Status"] == "won" else answer["Cost"]
    assert decision == fastpath_settle.Decision(success=True, charge=cost, generation=None, outcome="reaped_snapshot")


@pytest.mark.parametrize(
    ("success", "answer"),
    [
        (True, {"Status": "won", "Kind": "refund", "Charge": 0}),
        (False, {"Status": "won", "Kind": "settle", "Charge": 9}),
        (False, {"Status": "settled", "Outcome": "settled", "Cost": 9, "CostKnown": True}),
        (True, {"Status": "settled", "Outcome": "reaped_snapshot"}),
        (False, {"Status": "settled", "Outcome": "reaped_snapshot"}),
        (True, {"Status": "recorded"}),
    ],
    ids=["settle_lost_to_refund", "refund_lost_to_settle", "refund_after_a_closed_settle",
         "settle_after_a_reap_of_unknown_cost", "refund_after_a_reap_of_unknown_cost", "recorded"],
)
def test_what_the_lease_has_not_decided_for_this_terminal_stays_undecided(success: bool, answer: dict[str, Any]) -> None:
    with pytest.raises(fastpath_settle.Undecided):
        fastpath_settle.forward_terminal(
            _Terminal(answer), _authorization(), success=success, charge=300, generation=None,  # type: ignore[arg-type]
        )


def test_a_settle_after_a_closed_lease_settle_keeps_its_own_records() -> None:
    generation = Generation(id="gen-1", request_id="r", workspace_id=WORKSPACE, key_hash="k", model="m",
                            provider_name="p", app="a", tokens_prompt=1, tokens_completion=1,
                            total_cost_microdollars=300, usage_type="Credits", speed_tokens_per_second=1.0,
                            finish_reason="stop", status="success", streamed=True)
    decision = fastpath_settle.forward_terminal(
        _Terminal({"Status": "settled", "Outcome": "settled", "Cost": 280, "CostKnown": True}),  # type: ignore[arg-type]
        _authorization(), success=True, charge=300, generation=generation,
    )
    assert decision.outcome is None and decision.success and decision.charge == 280
    assert decision.generation is not None and decision.generation.total_cost_microdollars == 280


# -- the settle route and the drain ------------------------------------------


def test_a_settle_after_the_owner_reaped_records_the_snapshot(fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    fleet.answers["/v1/settle"] = (200, REAP_WON)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())["data"]
    assert data["disposition"] == "reaped_snapshot" and data["finalization_outcome"] == "reaped_snapshot"
    assert data["already_settled"] is True and data["settled"] is True
    _assert_reaped(store, database, key, authorized["authorization_id"], 77)
    intents = [r for r in database.settle_outbox.values() if r["authorization_id"] == authorized["authorization_id"]]
    assert [r["status"] for r in intents] == ["done"]


def test_a_reap_is_booked_in_credits_whatever_the_terminal_asked(fleet: _Fleet) -> None:
    """The lease reaped the stream's snapshot in credits: a BYOK settle that
    meets the reap records credits, and books the key's credit usage."""
    from trusted_router.storage_models import SettleOutboxRow

    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    fleet.answers["/v1/settle"] = (200, REAP_WON)
    intent = SettleOutboxRow(authorization_id=authorization.id, intent_kind="settle", settle_origin="typed",
                             actual_cost_micro=306, selected_usage_type="BYOK")
    gateway._settle_leased_authorization(
        authorization, settings=_pilot(), success=True, actual_cost=306, selected_usage_type="BYOK",
        generation=None, refund_benchmark=None, settle_intent=intent, typed_store=gateway.typed_billing_store(store),
        background_tasks=None, customer_billing_event=True, generation_model_id="m", generation_provider="anthropic",
    )
    finalized = store.get_gateway_authorization(authorization.id)
    assert finalized is not None and finalized.finalization_outcome == "reaped_snapshot"
    assert finalized.finalized_usage_type == "Credits"
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["usage"] == 77


def test_a_refund_after_the_owner_reaped_records_the_snapshot(fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    fleet.answers["/v1/refund"] = (200, REAPED)
    gateway._settle_gateway_authorization(_settle_body(authorized, success=False), success=False, settings=_pilot())
    _assert_reaped(store, database, key, authorized["authorization_id"], 55)


def test_the_drain_records_a_reap_the_front_door_answers_later(fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    fleet.answers["/v1/settle"] = (200, {"Status": "recorded"})
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    (row,) = [r for r in database.settle_outbox.values() if r["authorization_id"] == authorized["authorization_id"]]
    assert row["status"] == "pending"
    row["next_attempt_at"] = "2020-01-01T00:00:00Z"
    fleet.answers["/v1/settle"] = (200, REAP_WON)
    settle_outbox_drain.drain_settle_outbox(10)
    _assert_reaped(store, database, key, authorized["authorization_id"], 77)
    (row,) = [r for r in database.settle_outbox.values() if r["authorization_id"] == authorized["authorization_id"]]
    assert row["status"] == "done"


# -- the reaper ---------------------------------------------------------------


def test_the_reaper_leaves_a_heartbeated_leased_row_to_its_lease(fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    now = _expire(database, authorization)
    result = store.reap_expired_reservations_result(now=now, limit=10, snapshot_booking_enabled=True)
    assert result.count == 0 and result.leased == (authorization.id,)
    assert result.outcome_counts["leased_stream"] == 1
    still = store.get_gateway_authorization(authorization.id)
    assert still is not None and not still.settled
    assert not database.reservations[authorization.credit_reservation_id]["settled"]
    assert "/v1/refund" not in fleet.paths()


def _reap_pass(fleet: _Fleet, disposition: tuple[int, Any]) -> tuple[Any, Any, Any, GatewayAuthorization]:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    _expire(database, authorization)
    fleet.answers["/v1/disposition"] = disposition
    settle_outbox_drain.drain_settle_outbox(10)
    return store, database, key, authorization


def test_a_reaped_disposition_records_the_snapshot_at_the_lease_cost(fleet: _Fleet) -> None:
    store, database, key, authorization = _reap_pass(fleet, (200, REAPED))
    assert ("/v1/disposition", {"Envelope": "sealed-pilot"}) in fleet.seen
    assert "/v1/refund" not in fleet.paths()
    _assert_reaped(store, database, key, authorization.id, 55)


@pytest.mark.parametrize("outcome", ["refunded", "released"])
def test_a_refunded_or_released_disposition_records_a_refund(fleet: _Fleet, outcome: str) -> None:
    store, database, key, authorization = _reap_pass(
        fleet, (200, {"Status": "settled", "Outcome": outcome, "CostKnown": True}),
    )
    finalized = store.get_gateway_authorization(authorization.id)
    assert finalized is not None and finalized.settled and finalized.finalization_outcome == "refunded"
    assert not _generations(database)
    assert "/v1/refund" not in fleet.paths()


@pytest.mark.parametrize(
    "disposition",
    [
        (404, {"error": "not found"}),
        (200, {"Status": "pending"}),
        (200, {"Status": "failed"}),
        (200, {"Status": "invalid"}),
        (200, {"Status": "settled", "Outcome": "reaped_snapshot"}),
        (200, {"Status": "settled", "Outcome": "pending"}),
        (502, {"error": "owner"}),
        (200, ["not", "an", "object"]),
        (0, None),
    ],
    ids=["older_front_door_404", "pending", "failed", "invalid", "reap_of_unknown_cost", "pending_outcome",
         "error_status", "malformed", "timeout"],
)
def test_every_other_answer_is_pending_and_pushes_the_expiry_never_a_refund(
    fleet: _Fleet, disposition: tuple[int, Any],
) -> None:
    store, database, _key, authorization = _reap_pass(fleet, disposition)
    still = store.get_gateway_authorization(authorization.id)
    assert still is not None and not still.settled
    reservation = database.reservations[authorization.credit_reservation_id]
    assert not reservation["settled"]
    expires = reservation["expires_at"]
    expires = expires if isinstance(expires, dt.datetime) else dt.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    assert expires > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=250)
    assert "/v1/refund" not in fleet.paths()
    assert not _generations(database)


def test_an_older_front_doors_404_moves_no_breaker(fleet: _Fleet) -> None:
    store, database, key = _seed()
    client = gateway._fastpath_client
    assert client is not None
    fleet.answers["/v1/disposition"] = (404, {})
    for _ in range(fd.BREAKER_THRESHOLD + 2):
        with pytest.raises(fd.FrontDoorUnavailable):
            client.disposition(envelope="sealed-pilot")
    # The front door is still tried: an authorize reaches it.
    assert client.authorize(workspace_id=WORKSPACE, request="r", estimate=1, stream=False, boot=b"b") is not None


def test_no_front_door_is_pending(monkeypatch: pytest.MonkeyPatch, fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = _leased_stream(store, database, key)
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    now = _expire(database, authorization)
    outcome = fastpath_settle.reconcile_reaped(None, store, authorization.id, now=now, grace_seconds=300)
    assert outcome == fastpath_settle.RECONCILED_PENDING
    still = store.get_gateway_authorization(authorization.id)
    assert still is not None and not still.settled


def test_a_settle_after_the_auditors_zero_reap_of_a_plain_hold_keeps_its_records(fleet: _Fleet) -> None:
    """Through the route, a leased request no heartbeat reached: its settle
    answered with the closed lease's uncharged reap records the settle's own
    generation and activity row at the lease's cost, outcome settled, as
    before; a refund answered so stays durable and pending, as before."""
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    fleet.answers["/v1/settle"] = (200, {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 0, "CostKnown": True})
    gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
    finalized = store.get_gateway_authorization(authorized["authorization_id"])
    assert finalized is not None and finalized.settled
    assert finalized.finalization_outcome == "settled" and finalized.finalized_cost_microdollars == 0
    (generation,) = _generations(database)
    assert finalized.finalized_generation_id == generation["id"] and generation.get("settled_from") != "heartbeat"
    assert len(database.operational_analytics_outbox) == 1

    second = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    fleet.answers["/v1/refund"] = (200, {"Status": "settled", "Outcome": "reaped_snapshot", "Cost": 0, "CostKnown": True})
    data = gateway._settle_gateway_authorization(_settle_body(second, success=False), success=False,
                                                 settings=_pilot())["data"]
    assert data["disposition"] == "intent_durable"
    still = store.get_gateway_authorization(second["authorization_id"])
    assert still is not None and not still.settled


class _ReconcileStore:
    """A store the reconcile reads one row from, recording every write."""

    def __init__(self, authorization: GatewayAuthorization | None) -> None:
        self.authorization = authorization
        self.writes: list[str] = []

    def get_gateway_authorization(self, _authorization_id: str) -> GatewayAuthorization | None:
        return self.authorization

    def extend_reservation_expiry(self, *_args: Any) -> bool:
        self.writes.append("extend")
        return True

    def typed_finalize_leased_result(self, *_args: Any, **_kwargs: Any) -> None:
        self.writes.append("finalize")


class _Disposing:
    def __init__(self, answer: fd.Disposition) -> None:
        self.answer, self.asked = answer, 0

    def disposition(self, **_kwargs: Any) -> fd.Disposition:
        self.asked += 1
        return self.answer


@pytest.mark.parametrize("row", ["absent", "not_leased", "settled"])
def test_the_reconcile_leaves_a_row_that_is_not_an_open_leased_one_alone(row: str) -> None:
    authorization: GatewayAuthorization | None = _authorization()
    if row == "absent":
        authorization = None
    elif row == "not_leased":
        authorization = dataclasses.replace(_authorization(), fastpath_envelope=None, credit_reservation_id="r")
    else:
        authorization = dataclasses.replace(_authorization(), settled=True, credit_reservation_id="r")
    store = _ReconcileStore(authorization)
    client = _Disposing(fd.Disposition(status="pending", outcome="", cost=0, cost_known=False))
    outcome = fastpath_settle.reconcile_reaped(client, store, "gwa-1", now=dt.datetime.now(dt.UTC),  # type: ignore[arg-type]
                                               grace_seconds=300)
    assert outcome == fastpath_settle.RECONCILED_SKIPPED
    assert store.writes == [] and client.asked == 0


@pytest.mark.parametrize("intent", ["pending", "unreadable"])
def test_a_settled_disposition_with_its_intent_pending_pages_no_one(
    caplog: pytest.LogCaptureFixture, intent: str,
) -> None:
    """With the outbox guard off the reaper's scan passes over a row with a
    pending settle intent; that settle's intent writes its records, so the
    row is asked again later and nothing pages. A read of the intent that
    fails counts as pending too."""
    caplog.set_level(logging.INFO, logger="trusted_router.fastpath")
    store = _ReconcileStore(dataclasses.replace(_authorization(), credit_reservation_id="r"))
    client = _Disposing(fd.Disposition(status="settled", outcome="settled", cost=306, cost_known=True))

    def has_intent(_authorization_id: str) -> bool:
        if intent == "unreadable":
            raise RuntimeError("store away")
        return True

    outcome = fastpath_settle.reconcile_reaped(client, store, "gwa-1", now=dt.datetime.now(dt.UTC),  # type: ignore[arg-type]
                                               grace_seconds=300, has_intent=has_intent)
    assert outcome == fastpath_settle.RECONCILED_PENDING
    assert store.writes == ["extend"]
    assert not [r for r in caplog.records if r.getMessage().startswith("fastpath.settled_without_intent ")]


def test_a_settled_disposition_without_an_intent_is_told_and_left(fleet: _Fleet, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.fastpath")
    store, database, _key, authorization = _reap_pass(
        fleet, (200, {"Status": "settled", "Outcome": "settled", "Cost": 306, "CostKnown": True}),
    )
    still = store.get_gateway_authorization(authorization.id)
    assert still is not None and not still.settled
    lines = [r for r in caplog.records if r.getMessage().startswith("fastpath.settled_without_intent ")]
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR and lines[0].name == "trusted_router.fastpath"
    assert authorization.id in lines[0].getMessage()


def test_a_disposition_pending_past_the_end_of_life_is_told(fleet: _Fleet, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.fastpath")
    # The fleet's admission gave an end of life in 2026-10-14, long past.
    _reap_pass(fleet, (200, {"Status": "pending"}))
    assert [r for r in caplog.records if r.getMessage().startswith("fastpath.leased_reap_overdue ")]


def test_a_leased_row_with_no_heartbeat_is_refunded_as_today(fleet: _Fleet) -> None:
    store, database, key = _seed()
    authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    authorization = store.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    now = _expire(database, authorization)
    result = store.reap_expired_reservations_result(now=now, limit=10, snapshot_booking_enabled=True)
    assert result.count == 1 and result.leased == ()
    finalized = store.get_gateway_authorization(authorization.id)
    assert finalized is not None and finalized.finalization_outcome == "refunded"
    assert "/v1/disposition" not in fleet.paths()
