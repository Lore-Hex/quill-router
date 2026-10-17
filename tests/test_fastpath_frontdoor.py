"""The fast path's front door client (docs/design/fast-admission-pilot.md)."""

from __future__ import annotations

import base64
import json
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from trusted_router.services import fastpath_frontdoor as fd


class _Door:
    """One front door's answers, by path, and the requests it saw."""

    def __init__(self, answers: dict[str, Any] | None = None, *, status_code: int = 200, raw: bytes | None = None) -> None:
        self.answers = answers or {}
        self.status_code = status_code
        self.raw = raw
        self.seen: list[tuple[str, dict[str, Any]]] = []


def _transport(doors: dict[str, _Door], *, down: AbstractSet[str] = frozenset()) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        address = request.url.netloc.decode()
        if address in down:
            raise httpx.ConnectError("down", request=request)
        door = doors[address]
        door.seen.append((request.url.path, json.loads(request.content)))
        if door.raw is not None:
            return httpx.Response(door.status_code, content=door.raw)
        return httpx.Response(door.status_code, json=door.answers[request.url.path])

    return httpx.MockTransport(handle)


def _client(doors: dict[str, _Door], *, down: AbstractSet[str] = frozenset(), clock: list[float] | None = None) -> fd.FrontDoorClient:
    ticks = clock if clock is not None else [0.0]
    return fd.FrontDoorClient(
        lambda _budget: tuple(doors),
        timeout_seconds=1.5,
        clock=lambda: ticks[0],
        transport=_transport(doors, down=down),
    )


ADMIT = {"Status": "admitted", "Envelope": "sealed", "EndOfLife": "2026-10-14T05:00:00.123456789Z"}


def test_an_admitted_authorize_carries_the_envelope_and_the_end_of_life() -> None:
    door = _Door({"/v1/authorize": ADMIT})
    client = _client({"10.0.0.1:8080": door})
    got = client.authorize(workspace_id="ws", request="req-1", estimate=42, stream=False, boot=b"boot-1")
    assert got == fd.Admitted(envelope="sealed", end_of_life=datetime(2026, 10, 14, 5, 0, 0, 123456, tzinfo=UTC))
    path, body = door.seen[0]
    assert path == "/v1/authorize"
    assert body == {"Workspace": "ws", "Request": "req-1", "Estimate": 42, "Stream": False, "Boot": base64.b64encode(b"boot-1").decode()}


@pytest.mark.parametrize("status", ["off", "busy", "invalid", "failed"])
def test_every_other_answer_is_todays_path(status: str) -> None:
    client = _client({"10.0.0.1:8080": _Door({"/v1/authorize": {"Status": status}})})
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None


def test_an_admitted_answer_without_an_envelope_is_todays_path() -> None:
    client = _client({"10.0.0.1:8080": _Door({"/v1/authorize": {"Status": "admitted", "Envelope": ""}})})
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None


def test_a_request_the_front_door_would_refuse_is_not_sent() -> None:
    door = _Door({"/v1/authorize": ADMIT})
    client = _client({"10.0.0.1:8080": door})
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"") is None
    assert client.authorize(workspace_id="", request="r", estimate=1, stream=False, boot=b"b") is None
    assert client.authorize(workspace_id="ws", request="r", estimate=-1, stream=False, boot=b"b") is None
    assert door.seen == []


def test_a_front_door_that_is_down_is_passed_over_for_the_next() -> None:
    up = _Door({"/v1/authorize": ADMIT})
    client = _client({"10.0.0.1:8080": _Door(), "10.0.0.2:8080": up}, down={"10.0.0.1:8080"})
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is not None
    assert len(up.seen) == 1


def test_no_front_door_at_all_is_todays_path() -> None:
    client = fd.FrontDoorClient(lambda _budget: (), timeout_seconds=1.5, transport=_transport({}))
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None


def test_calls_rotate_over_the_front_doors() -> None:
    a = _Door({"/v1/authorize": ADMIT})
    b = _Door({"/v1/authorize": ADMIT})
    client = _client({"10.0.0.1:8080": a, "10.0.0.2:8080": b})
    for _ in range(4):
        client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b")
    assert (len(a.seen), len(b.seen)) == (2, 2)


def test_the_breaker_skips_a_failing_front_door_and_tries_it_again_after_the_cooldown() -> None:
    bad = _Door(status_code=502, raw=b"owner unreachable")
    good = _Door({"/v1/authorize": ADMIT})
    clock = [0.0]
    client = _client({"10.0.0.1:8080": bad, "10.0.0.2:8080": good}, clock=clock)
    for _ in range(2 * fd.BREAKER_THRESHOLD):
        assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is not None
    # Every call reached the good door; the bad one was tried until the
    # breaker opened at the threshold, then skipped.
    assert len(bad.seen) == fd.BREAKER_THRESHOLD
    # Many more calls before the cooldown: the open breaker lets none through.
    for _ in range(4 * fd.BREAKER_THRESHOLD):
        assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is not None
    assert len(bad.seen) == fd.BREAKER_THRESHOLD
    clock[0] += fd.BREAKER_COOLDOWN_SECONDS
    # After the cooldown the door is tried again, on the calls that start
    # with it (the start rotates), and opens again at the threshold.
    for _ in range(4 * fd.BREAKER_THRESHOLD):
        assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is not None
    assert len(bad.seen) == 2 * fd.BREAKER_THRESHOLD


def test_one_call_tries_at_most_two_front_doors(monkeypatch: pytest.MonkeyPatch) -> None:
    doors = {f"10.0.0.{i}:8080": _Door() for i in range(1, 6)}
    client = _client(doors, down=set(doors))
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None
    # Down doors raise before the handler records them, so count the breakers.
    assert sum(b.failures for b in client._breakers.values()) == fd.DOORS_PER_CALL


def test_one_call_stays_within_its_budget_whatever_the_doors_do() -> None:
    """Each door's request is given only what is left of the call's budget,
    and a door reached after the budget is spent is not tried at all."""
    clock = [0.0]
    seen_timeouts: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 1.0  # the door answered slowly
        raise httpx.ReadTimeout("slow", request=request)

    client = fd.FrontDoorClient(
        lambda _budget: ("10.0.0.1:8080", "10.0.0.2:8080"), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(handle),
    )
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None
    assert [round(t, 2) for t in seen_timeouts] == [1.5, 0.5]
    clock[0] = 0.0
    seen_timeouts.clear()

    def slower(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 2.0
        raise httpx.ReadTimeout("slower", request=request)

    client = fd.FrontDoorClient(
        lambda _budget: ("10.0.0.1:8080", "10.0.0.2:8080"), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(slower),
    )
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None
    assert [round(t, 2) for t in seen_timeouts] == [1.5]


def test_a_slow_membership_read_cannot_hold_the_caller_past_the_budget() -> None:
    import time as real_time

    def slow_doors(_budget: float) -> tuple[str, ...]:
        real_time.sleep(2.0)
        return ("10.0.0.1:8080",)

    client = fd.FrontDoorClient(slow_doors, timeout_seconds=0.2, transport=_transport({"10.0.0.1:8080": _Door({"/v1/authorize": ADMIT})}))
    started = real_time.monotonic()
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None
    assert real_time.monotonic() - started < 1.0


def test_with_every_worker_busy_a_call_is_todays_path_at_once_and_is_never_sent_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """With every worker's permit held, a call takes none: it asks for one
    without waiting, starts no thread and sends nothing, then or later. The
    test holds the permits itself, so no schedule frees one; a race of
    holder threads against a sleep left one free on a slow runner."""
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json=ADMIT)

    client = fd.FrontDoorClient(lambda _b: ("10.0.0.1:8080",), timeout_seconds=1.5, transport=httpx.MockTransport(handle))
    permits = client._permits
    for _ in range(fd.WORKERS):
        assert permits.acquire(blocking=False)
    asked: list[tuple[tuple[object, ...], dict[str, object], bool]] = []

    class Spy:
        def acquire(self, *args: Any, **kwargs: Any) -> bool:
            got = permits.acquire(*args, **kwargs)
            asked.append((args, kwargs, got))
            return got

        def release(self) -> None:
            permits.release()

    started: list[object] = []
    real_thread = fd.threading.Thread

    def counted(*args: Any, **kwargs: Any) -> Any:
        if getattr(kwargs.get("target"), "__module__", None) == fd.__name__:
            started.append(kwargs.get("target"))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(client, "_permits", Spy())
    monkeypatch.setattr(fd.threading, "Thread", counted)
    assert client.authorize(workspace_id="ws", request="late", estimate=1, stream=False, boot=b"b") is None
    assert asked == [((), {"blocking": False}, False)], "asked once, without waiting, and refused"
    assert started == [] and sent == [], "nothing started, nothing sent"
    monkeypatch.undo()
    # Freed, the permits leave nothing behind that sends the call later, and
    # a call now takes a worker and is sent: the refusal was the permits'.
    for _ in range(fd.WORKERS):
        permits.release()
    assert sent == []
    assert client.authorize(workspace_id="ws", request="next", estimate=1, stream=False, boot=b"b") is not None
    assert sent == ["/v1/authorize"]
    client.close()


def test_a_late_answer_changes_no_breaker_either_way() -> None:
    clock = [0.0]

    def answers_late(request: httpx.Request) -> httpx.Response:
        clock[0] += 1.6  # the door answers after the caller's budget
        return httpx.Response(200, json=ADMIT)

    client = fd.FrontDoorClient(
        lambda _b: ("10.0.0.1:8080",), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(answers_late),
    )
    breaker = client._breakers.setdefault("10.0.0.1:8080", fd._Breaker())
    breaker.failures = 4
    deadline = clock[0] + 1.5
    # A good answer, late: not health (no reset) and not a failure (no open).
    assert client._call_within("/v1/authorize", {"Workspace": "ws"}, deadline) is None
    assert breaker.failures == 4

    def fails_late(request: httpx.Request) -> httpx.Response:
        clock[0] += 1.6
        raise httpx.ReadTimeout("slow", request=request)

    client = fd.FrontDoorClient(
        lambda _b: ("10.0.0.1:8080",), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(fails_late),
    )
    breaker = client._breakers.setdefault("10.0.0.1:8080", fd._Breaker())
    breaker.failures = 4
    deadline = clock[0] + 1.5
    assert client._call_within("/v1/authorize", {"Workspace": "ws"}, deadline) is None
    assert breaker.failures == 4
    # A start past the deadline sends nothing at all.
    sent: list[str] = []

    def records(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json=ADMIT)

    client = fd.FrontDoorClient(
        lambda _b: ("10.0.0.1:8080",), timeout_seconds=1.5, clock=lambda: clock[0],
        transport=httpx.MockTransport(records),
    )
    assert client._call_within("/v1/authorize", {"Workspace": "ws"}, clock[0] - 0.1) is None
    assert sent == []


def test_a_call_whose_thread_cannot_start_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading as real_threading

    sent: list[str] = []

    def records(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json=ADMIT)

    client = fd.FrontDoorClient(lambda _b: ("10.0.0.1:8080",), timeout_seconds=0.5, transport=httpx.MockTransport(records))

    def cannot_start(self: Any) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(real_threading.Thread, "start", cannot_start)
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None
    monkeypatch.undo()
    # The permit came back, and the call that could not start never runs.
    assert client.authorize(workspace_id="ws", request="r2", estimate=1, stream=False, boot=b"b") is not None
    assert sent == ["/v1/authorize"]


def test_a_body_that_is_not_json_counts_as_a_failure() -> None:
    client = _client({"10.0.0.1:8080": _Door(raw=b"<html>")})
    assert client.authorize(workspace_id="ws", request="r", estimate=1, stream=False, boot=b"b") is None


def test_a_settle_carries_the_charge_and_the_full_record_and_reads_the_owners_decision() -> None:
    door = _Door({"/v1/settle": {"Status": "won", "Kind": "settle", "Charge": 7, "Outcome": "settle", "Cost": 7, "CostKnown": True}})
    client = _client({"10.0.0.1:8080": door})
    got = client.settle(envelope="sealed", charge=7, full=b'{"record":1}', money={"estimate": 9, "charge": 7})
    assert got == fd.Terminal(status="won", kind="settle", charge=7, outcome="settle", cost=7, cost_known=True)
    sent = door.seen[0][1]
    assert sent == {
        "Envelope": "sealed",
        "Charge": 7,
        "Full": base64.b64encode(b'{"record":1}').decode(),
        "Money": base64.b64encode(b'{"charge":7,"estimate":9}').decode(),
    }
    # Both are Go []byte fields: base64 text that decodes to the bytes sent.
    assert json.loads(base64.b64decode(sent["Money"])) == {"charge": 7, "estimate": 9}
    assert base64.b64decode(sent["Full"]) == b'{"record":1}'


def test_a_refund_carries_the_envelope() -> None:
    door = _Door({"/v1/refund": {"Status": "recorded"}})
    client = _client({"10.0.0.1:8080": door})
    assert client.refund(envelope="sealed", money={"charge": 0, "estimate": 3}).status == "recorded"
    assert door.seen[0] == ("/v1/refund", {"Envelope": "sealed", "Money": base64.b64encode(b'{"charge":0,"estimate":3}').decode()})


def test_a_terminal_no_front_door_answers_is_raised_never_dropped() -> None:
    client = _client({"10.0.0.1:8080": _Door()}, down={"10.0.0.1:8080"})
    with pytest.raises(fd.FrontDoorUnavailable):
        client.settle(envelope="sealed", charge=1, full=b"", money={})
    with pytest.raises(fd.FrontDoorUnavailable):
        client.refund(envelope="sealed", money={})


def test_a_malformed_terminal_answer_is_raised() -> None:
    client = _client({"10.0.0.1:8080": _Door({"/v1/settle": {"Charge": "seven"}})})
    with pytest.raises(fd.FrontDoorUnavailable):
        client.settle(envelope="sealed", charge=1, full=b"", money={})


class _Snapshot:
    def __init__(self, rows: list[tuple[str]] | Exception, timeouts: list[float | None]) -> None:
        self.rows = rows
        self.timeouts = timeouts

    def __enter__(self) -> _Snapshot:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute_sql(self, sql: str, timeout: float | None = None) -> list[tuple[str]]:
        assert sql == fd.FAST_PATH_FRONT_DOORS
        self.timeouts.append(timeout)
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


class _Database:
    def __init__(self) -> None:
        self.rows: list[tuple[str]] | Exception = []
        self.reads = 0
        self.timeouts: list[float | None] = []

    def snapshot(self) -> _Snapshot:
        self.reads += 1
        return _Snapshot(self.rows, self.timeouts)


def test_front_doors_are_the_serving_members_read_at_most_once_per_ttl() -> None:
    db = _Database()
    db.rows = [("10.0.0.1:8080",), ("10.0.0.2:8080",)]
    clock = [100.0]
    doors = fd.SpannerFrontDoors(db, clock=lambda: clock[0])
    assert doors(1.5) == ("10.0.0.1:8080", "10.0.0.2:8080")
    assert doors(1.5) == ("10.0.0.1:8080", "10.0.0.2:8080")
    assert db.reads == 1
    # The read is given the call's budget.
    assert db.timeouts == [1.5]
    db.rows = [("10.0.0.3:8080",)]
    clock[0] += fd.FRONT_DOORS_TTL_SECONDS
    assert doors(0.7) == ("10.0.0.3:8080",)
    assert db.reads == 2


def test_a_membership_read_that_fails_keeps_the_last_front_doors() -> None:
    db = _Database()
    db.rows = [("10.0.0.1:8080",)]
    clock = [0.0]
    doors = fd.SpannerFrontDoors(db, clock=lambda: clock[0])
    assert doors(1.5) == ("10.0.0.1:8080",)
    db.rows = RuntimeError("spanner down")
    clock[0] += fd.FRONT_DOORS_TTL_SECONDS
    assert doors(1.5) == ("10.0.0.1:8080",)
    # And a failed read is not retried on every request.
    assert doors(1.5) == ("10.0.0.1:8080",)
    assert db.reads == 2


def test_go_times_with_nanoseconds_and_without_a_zone_are_read_or_refused() -> None:
    assert fd._parse_time("2026-10-14T05:00:00Z") == datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
    assert fd._parse_time("2026-10-14T05:00:00.5Z") == datetime(2026, 10, 14, 5, 0, 0, 500000, tzinfo=UTC)
    assert fd._parse_time("2026-10-14T05:00:00") is None
    assert fd._parse_time("") is None
    assert fd._parse_time(None) is None


_ELIGIBLE: dict[str, Any] = {
    "enabled": True,
    "workspace_id": "ws-1",
    "pilot_workspace_ids": frozenset({"ws-1"}),
    "region": "us-central1",
    "pilot_region_ids": frozenset({"us-central1"}),
    "stream": False,
    "standard_endpoint_pricing": True,
    "credit_funded": True,
    "boot_verified": True,
    "service_tier": None,
    "key_capped": False,
    "budget_strict": False,
    "app_markup": False,
    "records_durable": True,
}


def test_the_narrowest_cohort_is_eligible_and_each_departure_names_itself() -> None:
    assert fd.pilot_reason(**_ELIGIBLE) is None
    for field, value, reason in [
        ("enabled", False, "disabled"),
        ("workspace_id", "ws-2", "workspace"),
        ("region", "us-east4", "region"),
        ("stream", True, "stream"),
        ("standard_endpoint_pricing", False, "pricing"),
        ("credit_funded", False, "funding"),
        ("boot_verified", False, "boot"),
        ("service_tier", "priority", "service_tier"),
        ("key_capped", True, "key_cap"),
        ("budget_strict", True, "key_cap"),
        ("app_markup", True, "markup"),
        ("records_durable", False, "records"),
    ]:
        assert fd.pilot_reason(**{**_ELIGIBLE, field: value}) == reason, field
