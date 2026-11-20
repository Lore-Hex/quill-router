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
from trusted_router.errors import ExpectedHTTPException
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


def _raw(seq: int = 1, *, output_tokens: int = 10, input_tokens: int | None = None) -> bytes:
    body = json.loads(_literal("heartbeat_request.json"))
    body["seq"] = seq
    body["usage"]["output_tokens"] = output_tokens
    if input_tokens is not None:
        body["usage"]["input_tokens"] = input_tokens
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
RETRY = {"Status": "retry"}

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


def test_a_prompt_above_its_estimate_reaches_the_owner_clamped_to_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row's transaction lets a prompt above its estimate (100) raise the
    cap (300) by that excess, so a 400-token prompt with 10 output tokens
    (420) runs on. The owner bounds a hold's running charge by the cap and
    never sees the prompt, so it is sent the charge clamped to the cap: what
    its reap would charge either way. The enclave is told the true figure."""
    _db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))

    answer = _beat(private, boot, _raw(input_tokens=400))

    assert answer == {"accepted": True, "seq": 1, "expires_at_ms": DEADLINE_MS, "cap_micro": 300, "running_micro": 420}
    body = door.bodies()[0]
    assert body["Running"] == 300 and body["Usage"] == 410


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
    # No echoed deadline: EchoGranted has the owner take the one it last
    # granted (docs/design/fast-admission-streams.md section 4.6, increment 4).
    assert set(body) == {"Envelope", "GatewaySeq", "Hash", "Usage", "Running", "EchoGranted"}
    assert body["EchoGranted"] is True
    assert "EchoGranted" not in door.bodies()[0]
    assert body["GatewaySeq"] == 2 and body["Usage"] == 120 and body["Running"] == 140
    assert base64.b64decode(body["Hash"]) == hashlib.sha256(_canonical(second)).digest()


def test_a_replay_is_forwarded_again_with_the_same_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner may not have the first attempt: a replay the transaction
    answers read-only still goes to the front door, byte for byte. The first
    request's send and its resend are both answered retry here, so it is a
    503 and the enclave's replay is what lands."""
    _db, private, boot = _store(leased=True)
    door = _Door(RETRY, RETRY, ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()
    with pytest.raises(HTTPException) as first:
        _beat(private, boot, raw)
    assert first.value.status_code == 503
    assert _beat(private, boot, raw)["expires_at_ms"] == DEADLINE_MS
    assert len(door.seen) == 3 and door.seen[0] == door.seen[1] == door.seen[2]


# -- a front door's retry, resent once within the budget ---------------------
#
# 2026-11-16 (Sentry QUILL-ROUTER-EE): an owner whose Pub/Sub acknowledgements
# ran past the front door's 500 ms owner wait answered retry, and the route
# answered 503 though the record was acknowledged moments later. The client
# now sends a heartbeat answered retry once more, the same bytes, within the
# same budget; the owner answers it as a replay of the first.


def test_a_retry_then_an_accepted_resend_answers_the_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control: retry once, then accepted. Without the resend
    this request is a 503 (the test above, before this change)."""
    db, private, boot = _store(leased=True)
    door = _Door(RETRY, ACCEPT)
    _use(monkeypatch, _client(door))
    raw = _raw()

    answer = _beat(private, boot, raw)

    assert answer == {"accepted": True, "seq": 1, "expires_at_ms": DEADLINE_MS, "cap_micro": 300, "running_micro": 120}
    # The resend is the first send byte for byte (the owner's replay key is
    # its seq and hash), and the row's transaction ran once.
    assert len(door.seen) == 2 and door.seen[0] == door.seen[1]
    assert db.gateway_authorizations[AUTH_ID]["heartbeat_seq"] == 1


def test_a_front_door_that_always_answers_retry_is_503_after_one_resend_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: every send answered retry, as an owner wait
    would (0.3 s each): two sends, never a third, then today's 503."""
    _db, private, boot = _store(leased=True)
    door = _Door(RETRY, delay=0.3)
    _use(monkeypatch, _client(door, timeout=1.5))
    began = time.monotonic()
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    elapsed = time.monotonic() - began
    assert raised.value.status_code == 503 and raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail["error"]["message"] == "Stage D heartbeat owner unavailable; retry"
    # forward() logged why; the enclave retries. Not a Sentry error (QUILL-ROUTER-EE).
    assert isinstance(raised.value, ExpectedHTTPException)
    assert len(door.seen) == 2 and door.seen[0] == door.seen[1]
    assert elapsed < 1.5


def test_a_resend_the_budget_cuts_short_is_503_by_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first retry answers at 0.7 s of a 1 s budget; the resend's
    accepted answer would come at 1.4 s. The 503 is the proof that the
    resend had only what was left of the budget (a fresh one would have
    taken the accept); the time is a second check, with 0.3 s of slack for a
    loaded machine and still 0.1 s short of the accept."""
    _db, private, boot = _store(leased=True)
    door = _Door(RETRY, ACCEPT, delay=0.7)
    _use(monkeypatch, _client(door, timeout=1.0))
    began = time.monotonic()
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    elapsed = time.monotonic() - began
    assert raised.value.status_code == 503
    assert len(door.seen) == 2
    assert elapsed < 1.0 + 0.3


class _SlowRelease(threading.BoundedSemaphore):
    """A permit pool whose release takes a moment, so a worker that wakes its
    caller before it releases leaves the caller to find the permit held."""

    def release(self, n: int = 1) -> None:
        time.sleep(0.05)
        super().release(n)


def test_the_resend_finds_the_first_sends_permit_free_with_one_permit_left() -> None:
    """The first send's worker hands its permit back before it wakes the
    caller: with every other heartbeat permit held, the resend still gets
    one, where waking first left it 'every worker busy' and the stream a 503."""
    door = _Door(RETRY, ACCEPT)
    client = fd.FrontDoorClient(
        lambda _budget: (DOOR,), timeout_seconds=1.5, transport=door.transport(), sleep=lambda _s: None,
    )
    client._heartbeat_permits = _SlowRelease(fd.HEARTBEAT_WORKERS)
    for _ in range(fd.HEARTBEAT_WORKERS - 1):
        assert client._heartbeat_permits.acquire(blocking=False)
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == "accepted" and got.resent
    assert len(door.seen) == 2


def test_a_resend_answered_with_a_refusal_is_that_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _db, private, boot = _store(leased=True)
    _use(monkeypatch, _client(_Door(RETRY, {"Status": "stale"})))
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 409
    assert raised.value.detail == _json("rejection_stale_seq.json")


@pytest.mark.parametrize("status", ["accepted", "stale", "decided", "deadline_passed", "rejected", "invalid", "busy"])
def test_only_retry_is_resent(status: str) -> None:
    answer = ACCEPT if status == "accepted" else {"Status": status}
    door = _Door(answer, ACCEPT)
    got = _client(door).heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == status and not got.resent
    assert len(door.seen) == 1


def test_a_quick_retry_is_resent_only_after_the_resend_pause() -> None:
    """A lease whose publishes are failing refuses a new heartbeat at once
    and resumes them after a backoff: the resend waits until
    HEARTBEAT_RESEND_AFTER_SECONDS after the first send, not a moment
    longer, and within the budget."""
    clock = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    door = _Door(RETRY, ACCEPT)
    client = fd.FrontDoorClient(
        lambda _budget: (DOOR,), timeout_seconds=1.5, clock=lambda: clock[0], transport=door.transport(), sleep=sleep,
    )
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == "accepted" and got.resent
    assert slept == [pytest.approx(fd.HEARTBEAT_RESEND_AFTER_SECONDS)]
    assert len(door.seen) == 2


def test_a_slow_retry_is_resent_at_once() -> None:
    """A retry that took the front door's owner wait is resent with no pause."""
    clock = [0.0]
    slept: list[float] = []
    door = _Door(RETRY, ACCEPT)
    inner = door.transport()

    def owner_wait(request: httpx.Request) -> httpx.Response:
        clock[0] += 0.5
        return inner.handle_request(request)

    client = fd.FrontDoorClient(
        lambda _budget: (DOOR,), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(owner_wait), sleep=slept.append,
    )
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == "accepted" and got.resent
    assert slept == []


def test_no_resend_with_less_than_the_floor_of_the_budget_left() -> None:
    clock = [0.0]
    door = _Door(RETRY, ACCEPT)
    inner = door.transport()

    def late(request: httpx.Request) -> httpx.Response:
        clock[0] = 1.5 - fd.HEARTBEAT_RESEND_FLOOR_SECONDS / 2
        return inner.handle_request(request)

    client = fd.FrontDoorClient(
        lambda _budget: (DOOR,), timeout_seconds=1.5, clock=lambda: clock[0], transport=httpx.MockTransport(late),
        sleep=lambda _s: pytest.fail("slept with no resend to send"),
    )
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got == fd.HeartbeatAnswer(status="retry", deadline=None, resent=False)
    assert len(door.seen) == 1


def test_the_resend_goes_to_the_next_front_door() -> None:
    """A front door that could not reach the owner is not the only way to
    it: the resend starts at the next serving front door in turn."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        address = request.url.netloc.decode()
        seen.append(address)
        return httpx.Response(200, json=RETRY if len(seen) == 1 else ACCEPT)

    client = fd.FrontDoorClient(
        lambda _budget: ("10.0.0.1:8080", "10.0.0.2:8080"), timeout_seconds=1.5,
        transport=httpx.MockTransport(handle), sleep=lambda _s: None,
    )
    got = client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert got.status == "accepted" and got.resent
    assert len(seen) == 2 and seen[0] != seen[1]


def test_an_unanswered_resend_raises_as_any_unanswered_heartbeat() -> None:
    calls = [0]

    def handle(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(200, json=RETRY)
        raise httpx.ConnectError("down", request=request)

    client = fd.FrontDoorClient(
        lambda _budget: (DOOR,), timeout_seconds=1.5, transport=httpx.MockTransport(handle), sleep=lambda _s: None,
    )
    with pytest.raises(fd.FrontDoorUnavailable):
        client.heartbeat(envelope="e", seq=2, digest=b"\x00" * 32, usage=1, running=1, basis=None)
    assert calls[0] == 2


def test_a_resend_is_logged_with_its_answer(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _db, private, boot = _store(leased=True)
    _use(monkeypatch, _client(_Door(RETRY, ACCEPT)))
    with caplog.at_level("WARNING", logger="trusted_router.fastpath"):
        _beat(private, boot, _raw())
    assert f"fastpath heartbeat resent authorization_id={AUTH_ID} seq=1 status=accepted" in caplog.text


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


def test_a_basis_that_cannot_be_built_after_the_row_committed_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row's transaction has committed by the time the basis is built:
    a failure there answers 503 with Retry-After, which the enclave retries
    as a replay, never a 500, and sends nothing to the front door."""
    db, private, boot = _store(leased=True)
    door = _Door(ACCEPT)
    _use(monkeypatch, _client(door))

    def broken(*_args: Any, **_kwargs: Any) -> bytes:
        raise ValueError("a price entry that does not encode")

    monkeypatch.setattr(fh, "basis", broken)
    with pytest.raises(HTTPException) as raised:
        _beat(private, boot, _raw())
    assert raised.value.status_code == 503 and raised.value.headers == {"Retry-After": "1"}
    assert door.seen == []
    assert db.gateway_authorizations[AUTH_ID]["heartbeat_seq"] == 1


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
    booking on, and decides nothing, leaving it to the reconcile that reads
    the lease's disposition (design section 6.4; increment 3)."""
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

    assert result.count == 0 and result.leased == (AUTH_ID,)
    assert result.snapshot_bookings == 0
    assert not db.reservations["reservation"]["settled"]
    assert not db.reservations["reservation"].get("actual_micro")
    stored = db.gateway_authorizations[AUTH_ID]
    assert not stored["settled"] and stored.get("finalization_outcome") is None
    assert not db.generation_records
