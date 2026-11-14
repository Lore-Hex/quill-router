"""Spanner channel failover: when it moves traffic, and when it must not.

The unit tests drive a FailoverChannel over fake channels with a fake clock
and run each probe round inline. The local-server tests use real gRPC on
127.0.0.1: a server that stops answering on one chosen connection while the
connection stays open, which is the shape of the November 10 and 12 stalls.
The SDK tests build a real google-cloud-spanner client offline: its lanes are
fakes that answer like Spanner, or real channels that are never asked to
connect. Nothing in this file leaves the machine.
"""

from __future__ import annotations

import functools
import gc
import logging
import threading
import time
from collections.abc import Callable, Iterator
from concurrent import futures
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from trusted_router import storage_gcp_channel_failover as failover
from trusted_router.storage_gcp_channel_failover import (
    CHECK_INTERVAL_SECONDS,
    FAILOVER_INTERVAL_SECONDS,
    OWN_CONNECTION,
    PROBE_INTERVAL_SECONDS,
    RETIRE_AFTER_SECONDS,
    STALL_SECONDS,
    ChannelProbe,
    FailoverChannel,
)

LOGGER = "trusted_router.storage_gcp_channel_failover"
PRIVATE_TEXT = "ya29.private-token from https://spanner.private:443"
EXECUTE_SQL = "/google.spanner.v1.Spanner/ExecuteSql"
STREAMING_SQL = "/google.spanner.v1.Spanner/ExecuteStreamingSql"
PROBE = ChannelProbe(method="/probe", request=b"probe")
OK = grpc.StatusCode.OK
DEADLINE = grpc.StatusCode.DEADLINE_EXCEEDED
NOT_FOUND = grpc.StatusCode.NOT_FOUND


class _Call(grpc.RpcError, grpc.Call, grpc.Future):
    """A call as gRPC hands it back: response or status, and done callbacks."""

    def __init__(self, code: Any = OK, response: Any = None) -> None:
        self._code = code
        self._response = response
        self._callbacks: list[Callable[[Any], None]] = []

    def finish(self, code: Any, response: Any = None) -> None:
        self._code, self._response = code, response
        for callback in self._callbacks:
            callback(self)

    # grpc.Future
    def result(self, timeout: Any = None) -> Any:
        if self._code is None:
            raise grpc.FutureTimeoutError()
        if self._code != OK:
            raise self
        return self._response

    def exception(self, timeout: Any = None) -> Any:
        return None if self._code == OK else self

    def traceback(self, timeout: Any = None) -> Any:
        return None

    def add_done_callback(self, fn: Callable[[Any], None]) -> None:
        if self._code is None:
            self._callbacks.append(fn)
        else:
            fn(self)

    def cancel(self) -> bool:
        if self._code is None:
            self.finish(grpc.StatusCode.CANCELLED)
        return True

    def cancelled(self) -> bool:
        return self._code == grpc.StatusCode.CANCELLED

    def running(self) -> bool:
        return self._code is None

    def done(self) -> bool:
        return self._code is not None

    # grpc.Call
    def code(self) -> Any:
        return self._code

    def details(self) -> str:
        return PRIVATE_TEXT

    def initial_metadata(self) -> Any:
        return ()

    def trailing_metadata(self) -> Any:
        return ()

    def is_active(self) -> bool:
        return self._code is None

    def time_remaining(self) -> Any:
        return None

    def add_callback(self, callback: Any) -> bool:
        return False


class _Callable:
    def __init__(self, channel: _Channel, method: str, streaming: bool) -> None:
        self._channel = channel
        self._method = method
        self._streaming = streaming

    def _send(self, request: Any, timeout: Any = None, metadata: Any = None, **_: Any) -> _Call:
        self._channel.sent.append((self._method, request, timeout))
        return self._channel.reply(self._method, request)

    def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        call = self._send(request, *args, **kwargs)
        return call if self._streaming else call.result()

    def with_call(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        call = self._send(request, *args, **kwargs)
        return call.result(), call

    def future(self, request: Any, *args: Any, **kwargs: Any) -> _Call:
        return self._send(request, *args, **kwargs)


class _Channel:
    """One real channel's worth of behavior; ``reply`` decides each answer."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[str, Any, Any]] = []
        self.subscribed: list[tuple[Any, bool]] = []
        self.closed = False
        self.reply: Callable[[str, Any], _Call] = lambda method, request: _Call(OK, method)

    def unary_unary(self, method: str, *_args: Any, **_kwargs: Any) -> _Callable:
        return _Callable(self, method, streaming=False)

    def unary_stream(self, method: str, *_args: Any, **_kwargs: Any) -> _Callable:
        return _Callable(self, method, streaming=True)

    def subscribe(self, callback: Any, try_to_connect: bool = False) -> None:
        self.subscribed.append((callback, try_to_connect))

    def unsubscribe(self, callback: Any) -> None:
        self.subscribed = [s for s in self.subscribed if s[0] != callback]

    def close(self) -> None:
        self.closed = True

    def calls(self, method: str = EXECUTE_SQL) -> int:
        return sum(1 for sent, _request, _timeout in self.sent if sent == method)


def _silent(_method: str, _request: Any) -> _Call:
    return _Call(DEADLINE)


def _answers_probe(method: str, request: Any) -> _Call:
    return _Call(NOT_FOUND) if method == PROBE.method else _Call(OK, method)


class _Rig(SimpleNamespace):
    channel: FailoverChannel
    channels: list[_Channel]
    clock: SimpleNamespace
    scheduled: list[tuple[float, Callable[[], None]]]

    def tick(self, seconds: float) -> None:
        self.clock.now += seconds

    def retirements(self) -> list[Callable[[], None]]:
        return [target for delay, target in self.scheduled if delay == RETIRE_AFTER_SECONDS]

    def checks(self) -> list[Callable[[], None]]:
        return [target for delay, target in self.scheduled if delay == CHECK_INTERVAL_SECONDS]


@pytest.fixture
def rig() -> Iterator[_Rig]:
    channels: list[_Channel] = []
    clock = SimpleNamespace(now=1000.0)
    scheduled: list[tuple[float, Callable[[], None]]] = []

    def create() -> _Channel:
        channels.append(_Channel(f"channel-{len(channels)}"))
        return channels[-1]

    channel = FailoverChannel(
        create,
        PROBE,
        clock=lambda: clock.now,
        spawn=lambda round_: round_(),
        schedule=lambda delay, target: scheduled.append((delay, target)),
    )
    yield _Rig(channel=channel, channels=channels, clock=clock, scheduled=scheduled)


def _messages(caplog: Any) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _fail(call: Any, times: int = 1, rig: _Rig | None = None) -> None:
    for _ in range(times):
        if rig is not None:
            rig.tick(0.5)
        with pytest.raises(grpc.RpcError):
            call(b"request", timeout=0.5)


def test_fails_over_after_deadlines_in_a_row_when_only_the_standby_answers(
    rig: _Rig, caplog: Any
) -> None:
    active, standby = rig.channels
    active.reply = _silent
    standby.reply = _answers_probe
    call = rig.channel.unary_unary(EXECUTE_SQL)

    def slow_probe(method: str, request: Any) -> _Call:
        rig.tick(0.2)
        return _Call(DEADLINE)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(call, 2, rig)
        assert len(rig.channels) == 2
        active.reply = lambda method, request: (
            slow_probe(method, request) if method == PROBE.method else _Call(DEADLINE)
        )
        _fail(call, 1, rig)

    # The multicallable made before the move now reaches the standby: GAPIC
    # keeps its stubs, so the choice is made per call.
    assert call(b"request") == EXECUTE_SQL
    assert (active.calls(), standby.calls()) == (3, 1)
    assert active.calls(PROBE.method) == standby.calls(PROBE.method) == 1
    assert _messages(caplog) == [
        "spanner channel failover client=primary reason=deadline_streak stalled_ms=1000 "
        "waiting=0 deadline_streak=3 probe_ms=200 failovers=1"
    ]
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    # A new standby is built, and the old channel is closed after the grace period.
    assert len(rig.channels) == 3
    (retire,) = rig.retirements()
    # Past the 20 second budget and BatchCreateSessions' 60 second default.
    assert RETIRE_AFTER_SECONDS > 60.0
    assert not active.closed
    retire()
    assert active.closed


def test_fails_over_when_a_call_has_waited_with_no_answer(rig: _Rig, caplog: Any) -> None:
    active, standby = rig.channels
    pending = _Call(code=None)
    active.reply = lambda method, request: pending if method == STREAMING_SQL else _Call(DEADLINE)
    standby.reply = _answers_probe
    stream = rig.channel.unary_stream(STREAMING_SQL)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert stream(b"request") is pending
        rig.tick(STALL_SECONDS - 0.1)
        assert stream(b"request") is pending  # still inside the window
        assert len(rig.channels) == 2
        rig.tick(0.1)
        # The call that notices still goes out on the old channel.
        assert stream(b"request") is pending

    assert _messages(caplog) == [
        "spanner channel failover client=primary reason=waiting stalled_ms=3000 "
        "waiting=3 deadline_streak=0 probe_ms=0 failovers=1"
    ]
    assert stream(b"request") is not pending
    # The stuck calls end when their deadline comes, without touching the new channel.
    pending.finish(DEADLINE)
    assert rig.channel._active.channel is standby


def test_spanner_wide_silence_never_moves_traffic(rig: _Rig, caplog: Any) -> None:
    active, standby = rig.channels
    active.reply = _silent
    standby.reply = _silent
    call = rig.channel.unary_unary(EXECUTE_SQL)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(call, 3, rig)
        _fail(call, 3, rig)  # inside the probe interval: no second round

    assert rig.channel._active.channel is active
    assert len(rig.channels) == 2 and not rig.retirements()
    assert active.calls(PROBE.method) == standby.calls(PROBE.method) == 1
    assert _messages(caplog) == [
        "spanner channel probe kept client=primary reason=deadline_streak standby=silent "
        "active=silent stalled_ms=1000 probe_ms=0"
    ]
    assert caplog.records[0].levelno == logging.INFO


def test_a_connection_that_answers_the_probe_keeps_its_traffic(rig: _Rig, caplog: Any) -> None:
    """Slow statements on a live connection: the cheap call still gets through."""
    active, standby = rig.channels
    standby.reply = _answers_probe
    pending = _Call(code=None)

    def active_reply(method: str, request: Any) -> _Call:
        if method == PROBE.method:
            rig.tick(0.1)
            return _Call(NOT_FOUND)
        return pending

    active.reply = active_reply
    call = rig.channel.unary_unary(EXECUTE_SQL)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        call.future(b"request")
        rig.tick(STALL_SECONDS)
        call.future(b"request")

    assert rig.channel._active.channel is active
    assert _messages(caplog) == [
        "spanner channel probe kept client=primary reason=waiting standby=answered "
        "active=answered stalled_ms=3000 probe_ms=100"
    ]
    # The probe's answer counts: the waiting calls are older than it now.
    rig.tick(PROBE_INTERVAL_SECONDS)
    call.future(b"request")
    assert len(_messages(caplog)) == 1


def test_a_real_answer_during_the_probe_keeps_the_traffic(rig: _Rig, caplog: Any) -> None:
    active, standby = rig.channels
    active.reply = _silent
    call = rig.channel.unary_unary(EXECUTE_SQL)

    def standby_reply(method: str, request: Any) -> _Call:
        if method == PROBE.method:
            # Meanwhile, a statement on the active channel comes back.
            rig.tick(0.1)
            active.reply = lambda m, r: _Call(DEADLINE) if m == PROBE.method else _Call(OK, m)
            assert call(b"request") == EXECUTE_SQL
            return _Call(NOT_FOUND)
        return _Call(OK, method)

    standby.reply = standby_reply
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(call, 3, rig)

    assert rig.channel._active.channel is active
    assert _messages(caplog) == [
        "spanner channel probe kept client=primary reason=deadline_streak standby=answered "
        "active=silent stalled_ms=1000 probe_ms=100"
    ]


def test_a_stream_that_ends_ok_is_an_answer_and_leaves_nothing_in_flight(rig: _Rig) -> None:
    """Most reads are ExecuteStreamingSql: a stream counts when it ends."""
    active = rig.channels[0]
    pending = _Call(code=None)
    active.reply = lambda method, request: pending if method == STREAMING_SQL else _Call(DEADLINE)
    lane = rig.channel._active
    _fail(rig.channel.unary_unary(EXECUTE_SQL), 2, rig)
    assert lane.deadline_streak == 2

    rig.tick(0.5)
    assert rig.channel.unary_stream(STREAMING_SQL)(b"request") is pending
    assert len(lane.inflight) == 1
    rig.tick(0.25)
    pending.finish(OK)

    assert lane.inflight == {}
    assert lane.last_answer == rig.clock.now
    assert lane.deadline_streak == 0


def test_a_future_that_ends_ok_is_an_answer_and_leaves_nothing_in_flight(rig: _Rig) -> None:
    active = rig.channels[0]
    pending = _Call(code=None)
    lane = rig.channel._active
    call = rig.channel.unary_unary(EXECUTE_SQL)
    active.reply = _silent
    _fail(call, 2, rig)

    active.reply = lambda method, request: pending
    rig.tick(0.5)
    assert call.future(b"request") is pending
    assert len(lane.inflight) == 1
    rig.tick(0.25)
    pending.finish(OK)

    assert lane.inflight == {}
    assert lane.last_answer == rig.clock.now
    assert lane.deadline_streak == 0


def test_a_stream_that_reaches_its_deadline_extends_the_streak(rig: _Rig) -> None:
    active, standby = rig.channels
    standby.reply = _answers_probe
    pending = _Call(code=None)
    active.reply = lambda method, request: pending if method == STREAMING_SQL else _Call(DEADLINE)
    _fail(rig.channel.unary_unary(EXECUTE_SQL), 2, rig)
    rig.channel.unary_stream(STREAMING_SQL)(b"request")

    pending.finish(DEADLINE)

    assert rig.channel._active.channel is standby


def test_the_stall_check_runs_while_calls_wait_and_never_when_idle(rig: _Rig, caplog: Any) -> None:
    """A stuck connection may see no call start or end: a timer looks instead."""
    active, standby = rig.channels
    standby.reply = _answers_probe
    pending = _Call(code=None)
    active.reply = lambda method, request: pending if method == STREAMING_SQL else _Call(DEADLINE)
    stream = rig.channel.unary_stream(STREAMING_SQL)
    assert rig.checks() == []

    with caplog.at_level(logging.INFO, logger=LOGGER):
        stream(b"request")
        stream(b"request")  # one timer, however many calls
        (check,) = rig.checks()
        rig.scheduled.clear()
        rig.tick(CHECK_INTERVAL_SECONDS)
        check()
        (check,) = rig.checks()  # still waiting: armed again
        rig.scheduled.clear()
        rig.tick(STALL_SECONDS - CHECK_INTERVAL_SECONDS)
        check()

    assert _messages(caplog) == [
        "spanner channel failover client=primary reason=waiting stalled_ms=3000 "
        "waiting=2 deadline_streak=0 probe_ms=0 failovers=1"
    ]
    # The timer already armed finds nothing in flight on the new channel and
    # stops; none runs again until a call starts.
    (check,) = rig.checks()
    rig.scheduled.clear()
    check()
    assert rig.checks() == []
    rig.channel.unary_unary(EXECUTE_SQL)(b"request")
    (check,) = rig.checks()
    rig.scheduled.clear()
    check()
    assert rig.checks() == []


def test_stalled_time_starts_with_the_first_call_that_got_no_answer(rig: _Rig, caplog: Any) -> None:
    """Not with the last answer: an idle connection has not been stalled all along."""
    rig.channels[0].reply = _silent
    rig.channels[1].reply = _answers_probe
    rig.tick(600.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(rig.channel.unary_unary(EXECUTE_SQL), 3, rig)

    assert _messages(caplog)[0].startswith(
        "spanner channel failover client=primary reason=deadline_streak stalled_ms=1000 "
    )


def test_an_answer_resets_the_deadline_streak(rig: _Rig) -> None:
    active, standby = rig.channels
    standby.reply = _answers_probe
    outcomes = iter([DEADLINE, DEADLINE, NOT_FOUND, DEADLINE, DEADLINE])
    active.reply = lambda method, request: _Call(next(outcomes))
    call = rig.channel.unary_unary(EXECUTE_SQL)

    _fail(call, 5, rig)

    assert active.calls(PROBE.method) == standby.calls(PROBE.method) == 0
    assert rig.channel._active.channel is active


def test_failovers_are_at_most_one_a_minute(rig: _Rig, caplog: Any) -> None:
    rig.channels[0].reply = _silent
    rig.channels[1].reply = _silent
    call = rig.channel.unary_unary(EXECUTE_SQL)

    def answer_then_go_silent(channel: _Channel) -> None:
        channel.reply = lambda method, request: (
            _Call(NOT_FOUND) if method == PROBE.method else _Call(DEADLINE)
        )

    answer_then_go_silent(rig.channels[1])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _fail(call, 3, rig)
        assert rig.channel._active.channel is rig.channels[1]
        # The new connection stalls too, and a fresh standby would answer.
        answer_then_go_silent(rig.channels[2])
        rig.channels[1].reply = _silent
        _fail(call, 10, rig)
        assert rig.channel._active.channel is rig.channels[1]
        assert rig.channels[2].calls(PROBE.method) == 0
        rig.tick(FAILOVER_INTERVAL_SECONDS - 10 * 0.5 - 0.5)
        _fail(call, 1, rig)

    assert rig.channel._active.channel is rig.channels[2]
    assert [m.split(" stalled_ms=")[0] for m in _messages(caplog)] == [
        "spanner channel failover client=primary reason=deadline_streak",
        "spanner channel failover client=primary reason=deadline_streak",
    ]
    assert _messages(caplog)[1].endswith(" failovers=2")


def test_probe_rounds_are_spaced_out(rig: _Rig, caplog: Any) -> None:
    rig.channels[0].reply = _silent
    rig.channels[1].reply = _silent
    call = rig.channel.unary_unary(EXECUTE_SQL)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(call, 3, rig)
        rig.tick(PROBE_INTERVAL_SECONDS - 0.25)
        _fail(call, 1)
        assert len(_messages(caplog)) == 1
        rig.tick(0.25)
        _fail(call, 1)

    assert len(_messages(caplog)) == 2
    assert rig.channels[1].calls(PROBE.method) == 2


def test_watchers_follow_the_active_channel(rig: _Rig) -> None:
    active, standby = rig.channels
    active.reply = _silent
    standby.reply = _answers_probe

    def watcher(_state: Any) -> None:
        return None

    rig.channel.subscribe(watcher, try_to_connect=False)
    assert active.subscribed == [(watcher, False)]
    _fail(rig.channel.unary_unary(EXECUTE_SQL), 3, rig)

    assert active.subscribed == []
    assert standby.subscribed == [(watcher, False)]
    rig.channel.unsubscribe(watcher)
    assert standby.subscribed == []
    assert rig.channel._subscriptions == []


def test_a_broken_probe_never_moves_traffic(rig: _Rig, caplog: Any) -> None:
    def broken(_method: str, _request: Any) -> _Call:
        raise RuntimeError(PRIVATE_TEXT)

    rig.channels[0].reply = _silent
    rig.channels[1].reply = broken
    call = rig.channel.unary_unary(EXECUTE_SQL)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fail(call, 3, rig)
        rig.tick(PROBE_INTERVAL_SECONDS)
        _fail(call, 1)

    assert rig.channel._active.channel is rig.channels[0]
    assert (
        _messages(caplog)
        == ["spanner channel probe unavailable client=primary error_class=RuntimeError"] * 2
    )


def test_lines_carry_fixed_words_and_numbers_only(rig: _Rig, caplog: Any) -> None:
    rig.channels[0].reply = _silent
    rig.channels[1].reply = _answers_probe
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _fail(rig.channel.unary_unary(EXECUTE_SQL), 3, rig)

    ours = [record for record in caplog.records if record.name == LOGGER]
    assert len(ours) == 1
    assert all(record.exc_info is None and record.stack_info is None for record in ours)
    assert "private" not in caplog.text
    assert all(isinstance(arg, str | int) for record in ours for arg in record.args or ())


@pytest.mark.parametrize(
    ("standby_reply", "expected"),
    [
        (
            _answers_probe,
            "spanner channel failover client=primary reason=deadline_streak stalled_ms=1000 "
            "waiting=0 deadline_streak=3 probe_ms=0 failovers=1",
        ),
        (
            _silent,
            "spanner channel probe kept client=primary reason=deadline_streak standby=silent "
            "active=silent stalled_ms=1000 probe_ms=0",
        ),
    ],
    ids=["failover", "probe-kept"],
)
def test_lines_stay_readable_on_the_settle_drain_worker(
    rig: _Rig, caplog: Any, standby_reply: Callable[[str, Any], _Call], expected: str
) -> None:
    # The worker scrubs every other module's record down to its logger and
    # level (2026-11-13: seven drain lines read "level=INFO exception_class=none").
    from trusted_router.services.settle_drain_observability import install_logging

    rig.channels[0].reply = _silent
    rig.channels[1].reply = standby_reply
    cleanup = install_logging()
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _fail(rig.channel.unary_unary(EXECUTE_SQL), 3, rig)
    finally:
        cleanup()
    assert _messages(caplog) == [expected]


def test_close_closes_every_channel(rig: _Rig) -> None:
    rig.channels[0].reply = _silent
    rig.channels[1].reply = _answers_probe
    _fail(rig.channel.unary_unary(EXECUTE_SQL), 3, rig)

    rig.channel.close()

    assert [channel.closed for channel in rig.channels] == [True, True, True]


def test_a_failed_replacement_is_built_by_the_next_round(caplog: Any) -> None:
    channels: list[_Channel] = []
    clock = SimpleNamespace(now=1000.0)
    broken = {"next": False}

    def create() -> _Channel:
        if broken["next"]:
            broken["next"] = False
            raise RuntimeError(PRIVATE_TEXT)
        channels.append(_Channel(f"channel-{len(channels)}"))
        channels[-1].reply = _answers_probe
        return channels[-1]

    channel = FailoverChannel(
        create,
        PROBE,
        clock=lambda: clock.now,
        spawn=lambda round_: round_(),
        schedule=lambda delay, target: None,
    )
    channels[0].reply = _silent
    call = channel.unary_unary(EXECUTE_SQL)
    broken["next"] = True
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            with pytest.raises(grpc.RpcError):
                call(b"request")
        assert channel._standby is None
        channels[1].reply = _silent
        clock.now += FAILOVER_INTERVAL_SECONDS
        for _ in range(3):
            with pytest.raises(grpc.RpcError):
                call(b"request")

    assert channel._active.channel is channels[2]
    assert _messages(caplog)[1] == (
        "spanner channel standby unavailable client=primary error_class=RuntimeError"
    )
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, True), ("1", True), ("true", True), ("0", False), ("false", False), (" OFF ", False)],
)
def test_kill_switch(monkeypatch: Any, value: str | None, enabled: bool) -> None:
    if value is None:
        monkeypatch.delenv(failover.ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(failover.ENABLED_ENV, value)

    assert failover.channel_failover_enabled() is enabled


def test_switched_off_leaves_the_sdk_to_build_its_own_client(caplog: Any) -> None:
    database = SimpleNamespace(_spanner_api=None)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.install_spanner_channel_failover(database, enabled=False)

    assert database._spanner_api is None
    assert _messages(caplog) == ["spanner channel failover off client=primary"]


class _Pool:
    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.binds = 0

    def bind(self, database: Any) -> None:
        self.binds += 1
        if self.failures:
            raise self.failures.pop(0)


def test_a_failed_bind_on_the_failover_client_is_retried_on_the_sdks(caplog: Any) -> None:
    closed: list[bool] = []
    transport = SimpleNamespace(close=lambda: closed.append(True))
    database = SimpleNamespace(
        _spanner_api=SimpleNamespace(transport=transport),
        _trusted_router_channel_failover=True,
    )
    pool = _Pool(RuntimeError(PRIVATE_TEXT))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.bind_session_pool(database, pool)

    assert pool.binds == 2
    assert closed == [True]
    assert database._spanner_api is None
    assert _messages(caplog) == [
        "spanner channel failover dropped client=primary error_class=RuntimeError"
    ]
    assert "private" not in caplog.text


def test_a_failed_bind_without_failover_is_the_callers() -> None:
    database = SimpleNamespace(_spanner_api=object())
    pool = _Pool(RuntimeError("no sessions"))
    with pytest.raises(RuntimeError, match="no sessions"):
        failover.bind_session_pool(database, pool)
    assert pool.binds == 1

    retried = SimpleNamespace(
        _spanner_api=SimpleNamespace(transport=SimpleNamespace(close=lambda: None)),
        _trusted_router_channel_failover=True,
    )
    pool = _Pool(RuntimeError("first"), RuntimeError("second"))
    with pytest.raises(RuntimeError, match="second"):
        failover.bind_session_pool(retried, pool)
    assert pool.binds == 2


def test_install_never_raises(caplog: Any) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.install_spanner_channel_failover(object())

    assert _messages(caplog) == [
        "spanner channel failover unavailable client=primary error_class=AttributeError"
    ]


# Real gRPC, one machine.


class _Server(grpc.GenericRpcHandler):
    """Answers until told to hold every stream on some connections, open and silent."""

    def __init__(self) -> None:
        self.peers: list[str] = []
        self.stuck: set[str] = set()
        self.stuck_everywhere = False
        self.release = threading.Event()
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=32))
        self._server.add_generic_rpc_handlers((self,))
        self.target = f"127.0.0.1:{self._server.add_insecure_port('127.0.0.1:0')}"
        self._server.start()

    def service(self, details: Any) -> Any:
        if details.method == "/test/Stream":
            return grpc.unary_stream_rpc_method_handler(self._stream)
        if details.method == "/test/Probe":
            return grpc.unary_unary_rpc_method_handler(self._probe)
        return grpc.unary_unary_rpc_method_handler(self._call)

    def _hold(self, context: Any) -> None:
        peer = context.peer()
        self.peers.append(peer)
        if self.stuck_everywhere or peer in self.stuck:
            # Past the caller's deadline: it gives up before this answers.
            self.release.wait(min((context.time_remaining() or 10.0) + 1.0, 10.0))

    def _call(self, request: bytes, context: Any) -> bytes:
        self._hold(context)
        return request

    def _stream(self, request: bytes, context: Any) -> Iterator[bytes]:
        self._hold(context)
        yield request
        yield request

    def _probe(self, request: bytes, context: Any) -> bytes:
        self._hold(context)
        context.abort(grpc.StatusCode.NOT_FOUND, "no such session")
        return b""

    def stop(self) -> None:
        self.release.set()
        self._server.stop(0)


@pytest.fixture
def server() -> Iterator[_Server]:
    running = _Server()
    yield running
    running.stop()


def _local(server: _Server, options: list[Any]) -> FailoverChannel:
    create = functools.partial(grpc.insecure_channel, server.target, options=options)
    return FailoverChannel(create, ChannelProbe(method="/test/Probe", request=b"", timeout=0.5))


def _wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _stall_first_connection(server: _Server, channel: FailoverChannel) -> str:
    call = channel.unary_unary("/test/Call")
    assert call(b"before", timeout=5) == b"before"
    stuck = server.peers[-1]
    server.stuck.add(stuck)
    for _ in range(3):
        with pytest.raises(grpc.RpcError) as raised:
            call(b"during", timeout=0.2)
        assert raised.value.code() == DEADLINE
    return stuck


def test_real_grpc_moves_off_a_connection_that_stopped_answering(
    server: _Server, caplog: Any
) -> None:
    channel = _local(server, [OWN_CONNECTION])
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            stuck = _stall_first_connection(server, channel)
            _wait_for(lambda: bool(_messages(caplog)))

        assert _messages(caplog)[0].startswith(
            "spanner channel failover client=primary reason=deadline_streak "
        )
        assert channel.unary_unary("/test/Call")(b"after", timeout=5) == b"after"
        assert list(channel.unary_stream("/test/Stream")(b"s", timeout=5)) == [b"s", b"s"]
        assert server.peers[-1] != stuck
        active = channel._active
        assert active.inflight == {}
        # A stream given up before its end leaves when it is cancelled...
        abandoned = channel.unary_stream("/test/Stream")(b"s", timeout=5)
        assert next(abandoned) == b"s"
        assert len(active.inflight) == 1
        abandoned.cancel()
        _wait_for(lambda: active.inflight == {})
        assert active.deadline_streak == 0
        # ...or, dropped without a cancel, when its deadline comes.
        dropped = channel.unary_stream("/test/Stream")(b"s", timeout=0.5)
        assert next(dropped) == b"s"
        assert len(active.inflight) == 1
        del dropped
        gc.collect()
        _wait_for(lambda: active.inflight == {})
    finally:
        channel.close()


def test_real_grpc_without_own_connections_the_standby_shares_the_stuck_one(
    server: _Server, caplog: Any
) -> None:
    """Positive control: gRPC's shared connection pool defeats a plain second channel."""
    channel = _local(server, [])
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _stall_first_connection(server, channel)
            _wait_for(lambda: bool(_messages(caplog)))

        assert _messages(caplog)[0].startswith(
            "spanner channel probe kept client=primary reason=deadline_streak "
            "standby=silent active=silent "
        )
        assert len(set(server.peers)) == 1
    finally:
        channel.close()


def test_real_grpc_server_wide_silence_keeps_the_connection(server: _Server, caplog: Any) -> None:
    channel = _local(server, [OWN_CONNECTION])
    try:
        call = channel.unary_unary("/test/Call")
        assert call(b"before", timeout=5) == b"before"
        server.stuck_everywhere = True
        with caplog.at_level(logging.INFO, logger=LOGGER):
            for _ in range(3):
                with pytest.raises(grpc.RpcError):
                    call(b"during", timeout=0.2)
            _wait_for(lambda: bool(_messages(caplog)))

        assert _messages(caplog)[0].startswith(
            "spanner channel probe kept client=primary reason=deadline_streak "
            "standby=silent active=silent "
        )
        server.stuck_everywhere = False
        assert call(b"after", timeout=5) == b"after"
        assert len(set(server.peers)) == 2  # the standby dialed, and carried nothing else
    finally:
        channel.close()


# The real SDK, offline.


class _SpannerLane(_Channel):
    """Stands in for one real channel under the SDK and answers like Spanner."""

    def __init__(self, tag: str, kwargs: dict[str, Any]) -> None:
        super().__init__(tag)
        self.kwargs = kwargs
        self.silent = False
        self.refuse_sessions = False
        self.unavailable_once: set[str] = set()
        self.reply = self._spanner

    def _spanner(self, method: str, request: Any) -> _Call:
        from google.cloud import spanner_v1

        name = method.rsplit("/", 1)[-1]
        if name == "GetSession" and request.name.endswith(failover.PROBE_SESSION_ID):
            return _Call(DEADLINE if self.silent else NOT_FOUND)
        if self.silent:
            return _Call(DEADLINE)
        if name in self.unavailable_once:
            self.unavailable_once.discard(name)
            return _Call(grpc.StatusCode.UNAVAILABLE)
        if name == "BatchCreateSessions" and self.refuse_sessions:
            return _Call(grpc.StatusCode.PERMISSION_DENIED)
        if name == "BatchCreateSessions":
            sessions = [
                spanner_v1.Session(name=f"{request.database}/sessions/{self.name}-{index}")
                for index in range(request.session_count)
            ]
            return _Call(OK, spanner_v1.BatchCreateSessionsResponse(session=sessions))
        if name == "GetSession":
            return _Call(OK, spanner_v1.Session(name=request.name))
        if name == "ExecuteSql":
            return _Call(OK, spanner_v1.ResultSet())
        return _Call(grpc.StatusCode.UNIMPLEMENTED)

    def requests(self, rpc: str) -> list[tuple[Any, Any]]:
        return [
            (request, timeout)
            for method, request, timeout in self.sent
            if method.rsplit("/", 1)[-1] == rpc
        ]


@pytest.fixture
def sdk_lanes(monkeypatch: Any) -> list[_SpannerLane]:
    """Each real channel the SDK would create becomes a fake lane, with its arguments."""
    from google.cloud.spanner_v1.services.spanner.transports.grpc import SpannerGrpcTransport

    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    lanes: list[_SpannerLane] = []

    def create_channel(cls: Any, host: str, **kwargs: Any) -> _SpannerLane:
        lanes.append(_SpannerLane(f"lane{len(lanes)}", {"host": host, **kwargs}))
        return lanes[-1]

    monkeypatch.setattr(SpannerGrpcTransport, "create_channel", classmethod(create_channel))
    return lanes


def _sdk_database(credentials: Any, pool: Any = None) -> Any:
    from google.cloud import spanner
    from google.cloud.spanner_v1.database import Database

    client = spanner.Client(
        project="project", credentials=credentials, disable_builtin_metrics=True
    )
    return Database("database", client.instance("instance"), pool=pool)


def _metadata_server_credentials() -> Any:
    """Production's credential class. Nothing here ever refreshes it."""
    from google.auth import compute_engine

    return compute_engine.Credentials(universe_domain="googleapis.com")


def _wait_for_message(caplog: Any, prefix: str) -> str:
    _wait_for(lambda: any(m.startswith(prefix) for m in _messages(caplog)))
    return next(m for m in _messages(caplog) if m.startswith(prefix))


def test_real_sdk_client_runs_on_the_failover_channel(
    sdk_lanes: list[_SpannerLane], caplog: Any
) -> None:
    """The store's construction order, on the real SDK, with fake lanes under it.

    The stubs GAPIC built at construction send on the active lane and follow
    a failover; the deadline wrapper still caps each RPC and keeps the
    generated retry; the session pool fills on one connection and its
    sessions are used on the other; the state watch moves with the traffic.
    """
    from google.api_core import exceptions
    from google.cloud.spanner_v1 import ExecuteSqlRequest, FixedSizePool

    from trusted_router.storage_gcp_io import (
        TXN_BUDGET_SECONDS,
        configure_spanner_rpc_deadlines,
        spanner_rpc_deadline,
    )
    from trusted_router.storage_gcp_transport_watch import watch_spanner_transport

    pool = FixedSizePool(size=2)
    database = _sdk_database(_metadata_server_credentials(), pool)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.install_spanner_channel_failover(database)
        pool.bind(database)
        configure_spanner_rpc_deadlines(database)
        watch_spanner_transport(database)

        api = database.spanner_api
        transport = api.transport
        assert [lane.kwargs for lane in sdk_lanes] == [sdk_lanes[0].kwargs] * 2
        arguments = sdk_lanes[0].kwargs
        # What the SDK passes its own create_channel, plus a connection per lane.
        assert arguments["host"] == "spanner.googleapis.com:443"
        assert arguments["credentials"] is transport._credentials
        assert ("grpc.keepalive_time_ms", 120000) in arguments["options"]
        assert arguments["options"][-1] == OWN_CONNECTION
        ((created, _timeout),) = sdk_lanes[0].requests("BatchCreateSessions")
        assert created.session_count == 2
        assert sdk_lanes[1].sent == []
        assert len(sdk_lanes[0].subscribed) == 1

        session = pool.get()
        assert session.name.rsplit("/", 1)[-1] in {"lane0-0", "lane0-1"}
        request = ExecuteSqlRequest(session=session.name, sql="SELECT 1")
        sdk_lanes[0].silent = True
        for _ in range(3):
            with pytest.raises(exceptions.DeadlineExceeded) as raised:
                api.execute_sql(request=request)
            assert getattr(raised.value, "tr_spanner_rpc_method", None) == "execute_sql"
        _wait_for_message(caplog, "spanner channel failover client=primary")

    # The same stubs and the same pooled session, on the other connection.
    assert session.exists()
    (get_session, _timeout) = sdk_lanes[1].requests("GetSession")[-1]
    assert get_session.name == session.name
    sdk_lanes[1].unavailable_once.add("ExecuteSql")
    with spanner_rpc_deadline(time.monotonic() + 5.0):
        api.execute_sql(request=request)
    attempts = sdk_lanes[1].requests("ExecuteSql")
    assert [sent.session for sent, _timeout in attempts] == [session.name] * 2
    assert all(0 < timeout <= 5.0 for _sent, timeout in attempts)
    api.execute_sql(request=request)
    assert 5.0 < sdk_lanes[1].requests("ExecuteSql")[-1][1] <= TXN_BUDGET_SECONDS
    assert sdk_lanes[0].subscribed == [] and len(sdk_lanes[1].subscribed) == 1
    assert len(sdk_lanes) == 3
    pool.put(session)
    transport.close()


def test_pool_fills_on_the_sdks_own_client_when_the_failover_client_fails(
    sdk_lanes: list[_SpannerLane], caplog: Any
) -> None:
    from google.cloud.spanner_v1 import FixedSizePool

    pool = FixedSizePool(size=2)
    database = _sdk_database(_metadata_server_credentials(), pool)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.install_spanner_channel_failover(database)
        sdk_lanes[0].refuse_sessions = True
        failover.bind_session_pool(database, pool)

    assert _messages(caplog) == [
        "spanner channel failover armed client=primary",
        "spanner channel failover dropped client=primary error_class=PermissionDenied",
    ]
    assert [lane.closed for lane in sdk_lanes[:2]] == [True, True]
    (sdk_own,) = sdk_lanes[2:]
    assert OWN_CONNECTION not in sdk_own.kwargs["options"]
    ((created, _timeout),) = sdk_own.requests("BatchCreateSessions")
    assert created.session_count == 2
    assert pool.get().name.rsplit("/", 1)[-1] in {"lane2-0", "lane2-1"}
    database.spanner_api.transport.close()


def test_every_lane_signs_with_the_credentials_the_watch_prepares(monkeypatch: Any) -> None:
    """Real channels, never asked to connect.

    Each one's auth plugin holds ``transport._credentials``, the object the
    watch sets to non-blocking refresh, including a standby built later.
    """
    import google.auth.transport.grpc as auth_grpc
    from google.auth import compute_engine

    from trusted_router.storage_gcp_transport_watch import _TimedRefresh, watch_spanner_transport

    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    signers: list[Any] = []
    options: list[Any] = []
    channels: list[FailoverChannel] = []

    real_plugin_init = auth_grpc.AuthMetadataPlugin.__init__

    def plugin_init(plugin: Any, credentials: Any, *args: Any, **kwargs: Any) -> None:
        signers.append(credentials)
        real_plugin_init(plugin, credentials, *args, **kwargs)

    real_secure_channel = grpc.secure_channel

    def secure_channel(target: str, credentials: Any, **kwargs: Any) -> Any:
        options.append(kwargs.get("options"))
        return real_secure_channel(target, credentials, **kwargs)

    class Recorded(FailoverChannel):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            channels.append(self)

    monkeypatch.setattr(auth_grpc.AuthMetadataPlugin, "__init__", plugin_init)
    monkeypatch.setattr(grpc, "secure_channel", secure_channel)
    monkeypatch.setattr(failover, "FailoverChannel", Recorded)

    database = _sdk_database(_metadata_server_credentials())
    failover.install_spanner_channel_failover(database)
    watch_spanner_transport(database)
    transport = database.spanner_api.transport
    try:
        (channel,) = channels
        channel._standby = None
        channel._replace_standby()  # what a failover builds
        signing = transport._credentials
        assert isinstance(signing, compute_engine.Credentials)
        assert len(signers) == 3
        assert all(signer is signing for signer in signers)
        assert signing._use_non_blocking_refresh is True
        assert isinstance(signing.refresh, _TimedRefresh)
        assert len(options) == 3
        assert all(
            OWN_CONNECTION in sent and ("grpc.keepalive_time_ms", 120000) in sent
            for sent in options
        )
        assert len(channel._subscriptions) == 1
    finally:
        transport.close()


def test_emulator_lanes_are_insecure_and_unsigned(monkeypatch: Any, caplog: Any) -> None:
    from google.cloud import spanner
    from google.cloud.spanner_v1.database import Database

    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines
    from trusted_router.storage_gcp_transport_watch import watch_spanner_transport

    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:9010")
    targets: list[tuple[str, Any]] = []
    real_insecure_channel = grpc.insecure_channel

    def insecure_channel(target: str, **kwargs: Any) -> Any:
        targets.append((target, kwargs.get("options")))
        return real_insecure_channel(target, **kwargs)

    monkeypatch.setattr(grpc, "insecure_channel", insecure_channel)
    database = Database("database", spanner.Client(project="project").instance("instance"))
    with caplog.at_level(logging.INFO):
        failover.install_spanner_channel_failover(database)
        configure_spanner_rpc_deadlines(database)
        watch_spanner_transport(database)
    transport = database.spanner_api.transport
    try:
        assert targets == [("localhost:9010", [OWN_CONNECTION])] * 2
        assert transport._credentials is None
        assert "spanner channel failover armed client=primary" in caplog.messages
        assert "spanner credential refresh unwatched client=primary" in caplog.messages
        assert hasattr(database.spanner_api.execute_sql, "__wrapped__")
    finally:
        transport.close()


def test_a_database_whose_client_is_already_built_is_left_alone(
    sdk_lanes: list[_SpannerLane], caplog: Any
) -> None:
    database = _sdk_database(_metadata_server_credentials())
    built = database.spanner_api
    with caplog.at_level(logging.INFO, logger=LOGGER):
        failover.install_spanner_channel_failover(database)

    assert database.spanner_api is built
    assert _messages(caplog) == ["spanner channel failover skipped client=primary reason=built"]
    built.transport.close()
