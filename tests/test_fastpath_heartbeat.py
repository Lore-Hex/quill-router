"""A leased stream's heartbeat, forwarded to the fast path's front door
(docs/design/fast-admission-streams.md, increment 1).

The route runs today's transaction on the row unchanged and then, for a row
with a lease's envelope only, sends the heartbeat to a front door. These
tests drive the real route over the fake store, with a real
FrontDoorClient whose transport is a fake front door, so what crosses the
wire is checked as bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from tests.test_stage_d_heartbeat import NOW, _boot, _boot_auth_header, _json, _literal, _request
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayHeartbeatRequest
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.services import fastpath_heartbeat as fh
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_authorize import reap_expired_reservations_result
from trusted_router.storage_gcp_counter_dml import insert_reservation
from trusted_router.storage_gcp_request_records import insert_gateway_authorization
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

ENVELOPE = "sealed-envelope-v1"
AUTH_ID = "gwa-stage-d-fixture"
DOOR = "10.0.0.1:8080"
# Go writes nanoseconds; the answer's expires_at_ms floors to the millisecond.
DEADLINE = "2026-09-02T00:00:30.123456789Z"
DEADLINE_MS = 1788307230123


class _Door:
    """A fake front door: an answer per call, in order, and every request's
    path and body bytes."""

    def __init__(self, *answers: dict[str, Any], delay: float = 0.0) -> None:
        self.answers = list(answers)
        self.delay = delay
        self.seen: list[tuple[str, bytes]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.seen.append((request.url.path, request.content))
            if self.delay:
                time.sleep(self.delay)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            return httpx.Response(200, json=answer)

        return httpx.MockTransport(handle)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for _path, raw in self.seen]


def _client(door: _Door, *, timeout: float = 5.0) -> fd.FrontDoorClient:
    return fd.FrontDoorClient(lambda _budget: (DOOR,), timeout_seconds=timeout, transport=door.transport())


def _authorization(*, leased: bool, boot_kid: str) -> GatewayAuthorization:
    return GatewayAuthorization(
        id=AUTH_ID,
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=300,
        credit_reservation_id="reservation",
        pricing_snapshot=_literal("pricing_document.json").decode().strip(),
        heartbeat_seq=0,
        stage_d_reason="ok",
        stage_d_prompt_tokens=100,
        stage_d_max_output_tokens=100,
        stage_d_boot_kid=boot_kid,
        fastpath_envelope=ENVELOPE if leased else None,
        fastpath_end_of_life="2026-09-02T02:20:30Z" if leased else None,
    )


def _store(*, leased: bool) -> tuple[FakeSpannerDatabase, Any, Any]:
    """The fake store with one Stage D row, leased or not (a leased row's
    reservation holds nothing, as _authorize_gateway_leased writes it), and
    the boot that signs its heartbeats."""
    private, boot = _boot("boot-a", "sha256:" + "aa" * 32)
    store, db = make_fake_store(request_record_write_mode="typed")
    db.now = NOW
    configure_store(store)
    authorization = _authorization(leased=leased, boot_kid=boot.kid)
    hold = 0 if leased else 300

    def seed(transaction: Any) -> None:
        insert_reservation(
            transaction,
            _ParamTypes,
            reservation_id="reservation",
            workspace_id="workspace",
            key_hash="key",
            ws_shard=0,
            credit_shard=0,
            key_shard=0,
            credit_reserved_micro=hold,
            key_reserved_micro=hold,
            hold_usage_type="Credits",
            authorization_id=AUTH_ID,
            idempotency_scope=None,
            idempotency_fingerprint=None,
            expires_at=NOW + timedelta(seconds=30),
        )
        insert_gateway_authorization(transaction, _ParamTypes, authorization, created_at=NOW)

    db.run_in_transaction(seed)
    store.observe_gateway_boot(boot)
    return db, private, boot


def _raw(seq: int = 1, *, output_tokens: int = 10) -> bytes:
    body = json.loads(_literal("heartbeat_request.json"))
    body["seq"] = seq
    body["usage"]["output_tokens"] = output_tokens
    return json.dumps(body, separators=(",", ":")).encode()


def _beat(private: Any, boot: Any, raw: bytes, settings: Settings | None = None) -> dict[str, Any]:
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    return gateway._heartbeat_gateway_sync(
        _request(_boot_auth_header(private, boot, raw)), body, settings or Settings(environment="test"), raw,
    )


def _canonical(raw: bytes) -> bytes:
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    return json.dumps(body.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":")).encode()


def _use(monkeypatch: pytest.MonkeyPatch, client: fd.FrontDoorClient | None) -> None:
    monkeypatch.setattr(gateway, "_fastpath_front_door", lambda _settings: client)


ACCEPT = {"Status": "accepted", "Deadline": DEADLINE}

# The basis a hold's first heartbeat carries, as bytes (design section 4.5).
EXPECTED_BASIS = (
    '{"app_markup_basis_points":0,"authorization_id":"gwa-stage-d-fixture",'
    '"boot":"' + base64.b64encode(b"boot-a").decode() + '",'
    '"endpoint_id":"anthropic/test","estimate":300,"key_hash":"key","model_id":"model",'
    '"price":{"endpoint_id":"anthropic/test","price_history_version":1,'
    '"rates":{"cache_creation_micro_per_million":1250000,"cached_input_micro_per_million":100000,'
    '"input_micro_per_million":1000000,"output_micro_per_million":2000000},'
    '"request_fee_micro":0,"rounding":"half_up_per_million",'
    '"tiers":[{"max_prompt_tokens":200000,"rates":{"cache_creation_micro_per_million":1250000,'
    '"cached_input_micro_per_million":100000,"input_micro_per_million":1000000,'
    '"output_micro_per_million":2000000}},{"max_prompt_tokens":null,"rates":'
    '{"cache_creation_micro_per_million":2500000,"cached_input_micro_per_million":200000,'
    '"input_micro_per_million":2000000,"output_micro_per_million":3000000}}]},'
    '"pricing_snapshot_sha256":"'
    + hashlib.sha256(_literal("pricing_document.json").decode().strip().encode()).hexdigest()
    + '","provider":"anthropic","receipt_fee_basis_points":0,'
    '"stage_d_max_output_tokens":100,"stage_d_prompt_tokens":100,"v":1,"workspace_id":"workspace"}'
).encode()


# -- the route ---------------------------------------------------------------


def test_an_accepted_leased_heartbeat_answers_the_owners_deadline_and_todays_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()

    answer = _beat(private, boot, raw)

    assert answer == {"accepted": True, "seq": 1, "expires_at_ms": DEADLINE_MS, "cap_micro": 300, "running_micro": 120}
    # Today's transaction ran unchanged first.
    stored = db.gateway_authorizations[AUTH_ID]
    assert stored["heartbeat_seq"] == 1
    assert stored["selected_endpoint_id"] == "anthropic/test"
    assert [path for path, _ in door.seen] == ["/v1/heartbeat"]


def test_the_first_heartbeat_sends_exact_bytes_with_the_basis_and_no_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()
    _beat(private, boot, raw)

    digest = hashlib.sha256(_canonical(raw)).digest()
    # The hash is the row's own heartbeat_hash, as bytes.
    assert digest == bytes.fromhex(db.gateway_authorizations[AUTH_ID]["heartbeat_hash"])
    expected = (
        '{"Envelope":"sealed-envelope-v1","GatewaySeq":1,'
        f'"Hash":"{base64.b64encode(digest).decode()}","Usage":110,"Running":120,'
        f'"Basis":"{base64.b64encode(EXPECTED_BASIS).decode()}"}}'
    ).encode()
    assert door.seen == [("/v1/heartbeat", expected)]


def test_the_basis_is_the_canonical_document_of_the_row() -> None:
    authorization = _authorization(leased=True, boot_kid="boot-a")
    assert fh.basis(authorization, selected_endpoint_id="anthropic/test") == EXPECTED_BASIS


def test_a_later_heartbeat_carries_no_basis_and_its_own_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))
    _beat(private, boot, _raw(1))
    second = _raw(2, output_tokens=20)
    answer = _beat(private, boot, second)

    assert answer["seq"] == 2 and answer["running_micro"] == 140
    body = door.bodies()[1]
    assert set(body) == {"Envelope", "GatewaySeq", "Hash", "Usage", "Running"}
    assert body["GatewaySeq"] == 2 and body["Usage"] == 120 and body["Running"] == 140
    assert base64.b64decode(body["Hash"]) == hashlib.sha256(_canonical(second)).digest()


def test_a_replay_is_forwarded_again_with_the_same_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner may not have the first attempt: a replay the transaction
    answers read-only still goes to the front door, byte for byte."""
    _db, private, boot = _store(leased=True)
    door = _Door({"Status": "retry"}, ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()
    with pytest.raises(HTTPException) as first:
        _beat(private, boot, raw)
    assert first.value.status_code == 503
    assert _beat(private, boot, raw)["expires_at_ms"] == DEADLINE_MS
    assert len(door.seen) == 2 and door.seen[0] == door.seen[1]


@pytest.mark.parametrize(
    ("status", "fixture"),
    [
        ("stale", "rejection_stale_seq.json"),
        ("decided", "rejection_already_terminal.json"),
        ("deadline_passed", "rejection_already_terminal.json"),
        ("rejected", "rejection_out_of_cohort.json"),
        ("invalid", "rejection_out_of_cohort.json"),
    ],
)
def test_each_refusal_is_todays_rejection_with_a_reason_the_enclave_knows(
    monkeypatch: pytest.MonkeyPatch, status: str, fixture: str,
) -> None:
    _db, private, boot = _store(leased=True)
    _use(monkeypatch, _client(_Door({"Status": status})))
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 409
    assert raised.value.detail == _json(fixture)


@pytest.mark.parametrize(
    "answer",
    [
        {"Status": "retry"},
        {"Status": "busy"},
        {"Status": "accepted"},
        {"Status": "accepted", "Deadline": "not a time"},
        {"Deadline": DEADLINE},
        {"Status": ""},
    ],
)
def test_retry_an_unknown_status_or_a_malformed_answer_is_503_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any],
) -> None:
    _db, private, boot = _store(leased=True)
    _use(monkeypatch, _client(_Door(answer)))
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}


def test_no_front_door_client_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _db, private, boot = _store(leased=True)
    _use(monkeypatch, None)
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 503


def test_a_front_door_slower_than_the_budget_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _db, private, boot = _store(leased=True)
    door = _Door(ACCEPT, delay=0.5)
    _use(monkeypatch, _client(door, timeout=0.05))
    began = time.monotonic()
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 503
    assert time.monotonic() - began < 0.45


def test_with_every_heartbeat_permit_held_a_heartbeat_is_503_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    client = _client(door)
    _use(monkeypatch, client)
    for _ in range(fd.HEARTBEAT_WORKERS):
        assert client._heartbeat_permits.acquire(blocking=False)
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 503
    assert door.seen == []


def test_the_two_permit_pools_are_apart() -> None:
    """Authorizes cannot starve heartbeats, nor heartbeats authorizes."""
    door = _Door({"Status": "admitted", "Envelope": "e", "EndOfLife": DEADLINE})
    client = _client(door)
    for _ in range(fd.HEARTBEAT_WORKERS):
        assert client._heartbeat_permits.acquire(blocking=False)
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=True, boot=b"b") is not None
    for _ in range(fd.HEARTBEAT_WORKERS):
        client._heartbeat_permits.release()
    for _ in range(fd.WORKERS):
        assert client._permits.acquire(blocking=False)
    door.answers = [ACCEPT]
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == "accepted"
    assert client._heartbeat_permits.acquire(blocking=False)


def test_a_rejected_transaction_never_reaches_the_front_door(monkeypatch: pytest.MonkeyPatch) -> None:
    db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))
    _beat(private, boot, _raw(1, output_tokens=20))
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw(2, output_tokens=10))
    assert raised.value.detail == _json("rejection_usage_regression.json")
    assert len(door.seen) == 1
    assert db.gateway_authorizations[AUTH_ID]["heartbeat_seq"] == 1


def test_the_kill_switch_answers_before_the_row_or_the_front_door(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    settings = Settings(environment="test", stage_d_heartbeat_enabled=False)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(gateway.heartbeat_gateway(_request(_boot_auth_header(private, boot, raw)), body, settings))
    assert raised.value.status_code == 503
    assert door.seen == []
    assert db.gateway_authorizations[AUTH_ID]["heartbeat_seq"] == 0


def test_a_row_without_an_envelope_is_todays_path_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """No front door is asked, and the answer is the transaction's own, the
    same bytes the route answered before this change."""
    db, private, boot = _store(leased=False)

    def no_front_door(_settings: Settings) -> fd.FrontDoorClient:
        raise AssertionError("a row without an envelope asked for a front door")

    monkeypatch.setattr(gateway, "_fastpath_front_door", no_front_door)
    answer = _beat(private, boot, _raw())
    expires = db.reservations["reservation"]["expires_at"]
    expected_ms = fh.epoch_millis(expires if expires.tzinfo else expires.replace(tzinfo=UTC))
    assert json.dumps(answer, separators=(",", ":")) == (
        f'{{"accepted":true,"seq":1,"expires_at_ms":{expected_ms},"cap_micro":300,"running_micro":120}}'
    )


# -- the client --------------------------------------------------------------


def test_the_client_reads_an_accepted_answer_with_nanoseconds() -> None:
    client = _client(_Door(ACCEPT))
    got = client.heartbeat(envelope="e", seq=3, digest=b"\x01" * 32, usage=5, running=7, basis=None)
    assert got == fd.HeartbeatAnswer(status="accepted", deadline=datetime(2026, 9, 2, 0, 0, 30, 123456, tzinfo=UTC))
    assert fh.epoch_millis(got.deadline) == DEADLINE_MS


@pytest.mark.parametrize("answer", [{"Status": "accepted"}, {"Status": "accepted", "Deadline": "never"}, {}])
def test_the_client_raises_on_an_answer_it_cannot_read(answer: dict[str, Any]) -> None:
    client = _client(_Door(answer))
    with pytest.raises(fd.FrontDoorUnavailable):
        client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=0, running=0, basis=None)


def test_the_client_raises_when_no_front_door_answers() -> None:
    client = fd.FrontDoorClient(lambda _budget: (), timeout_seconds=1.0, transport=_Door(ACCEPT).transport())
    with pytest.raises(fd.FrontDoorUnavailable):
        client.heartbeat(envelope="e", seq=1, digest=b"\x00" * 32, usage=0, running=0, basis=b"{}")


def test_a_held_heartbeat_permit_comes_back_when_its_call_ends() -> None:
    door = _Door(ACCEPT, delay=0.2)
    client = _client(door, timeout=0.05)
    with pytest.raises(fd.FrontDoorUnavailable):
        client.heartbeat(envelope="e", seq=1, digest=b"\x00" * 32, usage=0, running=0, basis=None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        held = [client._heartbeat_permits.acquire(blocking=False) for _ in range(fd.HEARTBEAT_WORKERS)]
        for got in held:
            if got:
                client._heartbeat_permits.release()
        if all(held):
            break
        threading.Event().wait(0.05)
    assert all(held)


# -- the reaper --------------------------------------------------------------


def test_the_reaper_never_books_a_leased_stream() -> None:
    """A leased stream whose row has heartbeated is the lease's to reap at
    its snapshot: the reaper books nothing for it, even with snapshot
    booking on (design section 6.4)."""
    db, private, boot = _store(leased=True)
    client = _client(_Door(ACCEPT))
    original = gateway._fastpath_front_door
    gateway._fastpath_front_door = lambda _settings: client  # type: ignore[assignment]
    try:
        _beat(private, boot, _raw())
    finally:
        gateway._fastpath_front_door = original  # type: ignore[assignment]
    assert db.gateway_authorizations[AUTH_ID]["started_at"] is not None

    result = reap_expired_reservations_result(
        db, _ParamTypes, now=datetime.now(UTC) + timedelta(seconds=301), snapshot_booking_enabled=True,
    )

    assert result.count == 1
    assert result.snapshot_bookings == 0
    assert db.reservations["reservation"]["actual_micro"] == 0
    stored = db.gateway_authorizations[AUTH_ID]
    assert stored["finalization_outcome"] == "refunded"
    assert not db.generation_records
