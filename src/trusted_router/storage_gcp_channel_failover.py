"""Move the main store's Spanner traffic off a connection that stopped answering.

Twice in three days (85 and 35 seconds) one control-plane instance's HTTP/2
connection to Spanner stopped answering streams while its siblings were
healthy. Every RPC on it reached its deadline, the 20 second billing budget
included, and then the connection carried on by itself. The channel stayed
READY throughout and the credential refresh was fine
(docs/incidents/2026-11-10-one-instance-spanner-stall.md), so gRPC never
noticed, and a shorter keepalive probably would not have either.

The store's ``SpannerClient`` is built on a :class:`FailoverChannel`: a
``grpc.Channel`` that holds two real channels, each on a connection of its
own, and sends every call to whichever one is active when the call starts.
GAPIC creates its stubs once, at transport construction, so the switch has to
live under the stubs, not above them.

A stall is suspected when the active connection has not answered for a while
with a call waiting, or after several deadlines in a row with no answer
between them. The same cheap RPC is then sent on both connections under a
short deadline. Traffic moves only when the standby answers and the active
connection does not. A Spanner-wide problem leaves the standby silent too, so
it never moves traffic: an application-side reset across the fleet is what
the October 15 report advised against. A slow but answering connection
answers the probe, so it does not move traffic either. At most one move a
minute. The old channel is closed 30 seconds later, by when every call it
carried has reached its deadline.

Only fixed words and numbers leave this module, as in the transport watch.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import grpc

logger = logging.getLogger(__name__)

ENABLED_ENV = "TR_SPANNER_CHANNEL_FAILOVER"
# The oldest call started since the active connection last answered.
STALL_SECONDS = 3.0
# Calls with a short deadline (the 500 ms admission reads) end before
# STALL_SECONDS; this many DEADLINE_EXCEEDED in a row says the same thing.
DEADLINE_STREAK = 3
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_INTERVAL_SECONDS = 5.0
FAILOVER_INTERVAL_SECONDS = 60.0
# Longer than the 20 second transaction budget, so closing the old channel
# cancels nothing a caller is still waiting on.
RETIRE_AFTER_SECONDS = 30.0
# gRPC shares one connection between channels with the same target and
# arguments. Without this the standby would ride the stuck connection.
OWN_CONNECTION = ("grpc.use_local_subchannel_pool", 1)
PROBE_METHOD = "/google.spanner.v1.Spanner/GetSession"
# No such session exists, so Spanner answers with an error (NOT_FOUND, per its
# API). Any status the server sends counts: the stream was carried both ways.
# Nothing is created and no lock is taken.
PROBE_SESSION_ID = "trusted-router-channel-probe"

# Statuses that are not the server's answer.
_UNANSWERED = frozenset(
    {
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.CANCELLED,
    }
)


def channel_failover_enabled() -> bool:
    """The kill switch, read once when the store is built. Default on."""
    value = os.environ.get(ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "off"}


@dataclass(frozen=True)
class ChannelProbe:
    """One cheap unary call, sent on a given channel under a short deadline."""

    method: str
    request: Any
    request_serializer: Callable[[Any], bytes] | None = None
    response_deserializer: Callable[[bytes], Any] | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    timeout: float = PROBE_TIMEOUT_SECONDS

    def start(self, channel: Any) -> Any:
        call = channel.unary_unary(
            self.method,
            request_serializer=self.request_serializer,
            response_deserializer=self.response_deserializer,
            _registered_method=True,
        )
        return call.future(self.request, timeout=self.timeout, metadata=self.metadata)


def _status(call: Any) -> Any:
    code = getattr(call, "code", None)
    try:
        return code() if callable(code) else None
    except Exception:  # noqa: BLE001 - bookkeeping never fails a call.
        return None


def _answered(status: Any) -> bool:
    return status is not None and status not in _UNANSWERED


def _answered_within(future: Any, timeout: float) -> bool:
    try:
        future.result(timeout=timeout)
    except grpc.RpcError as error:
        return _answered(_status(error))
    except Exception:  # noqa: BLE001 - a probe that did not finish is silent.
        future.cancel()
        return False
    return True


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="spanner-channel-probe", daemon=True).start()


def _schedule(delay: float, target: Callable[[], None]) -> None:
    timer = threading.Timer(delay, target)
    timer.daemon = True
    timer.start()


_Key = tuple[str, str, Any, Any, bool]


class _Lane:
    """One real channel and what the calls sent on it have done."""

    def __init__(self, channel: Any, now: float) -> None:
        self.channel = channel
        self.last_answer = now
        self.deadline_streak = 0
        self.inflight: dict[int, float] = {}
        self._callables: dict[_Key, Any] = {}

    def callable(self, key: _Key) -> Any:
        found = self._callables.get(key)
        if found is None:
            kind, method, request_serializer, response_deserializer, registered = key
            if kind == "unary_unary":
                factory = self.channel.unary_unary
            elif kind == "unary_stream":
                factory = self.channel.unary_stream
            elif kind == "stream_unary":
                factory = self.channel.stream_unary
            else:
                factory = self.channel.stream_stream
            found = self._callables.setdefault(
                key,
                factory(
                    method,
                    request_serializer=request_serializer,
                    response_deserializer=response_deserializer,
                    _registered_method=registered,
                ),
            )
        return found

    def answered(self, now: float) -> None:
        self.last_answer = max(self.last_answer, now)
        self.deadline_streak = 0

    def stall(self, now: float) -> str | None:
        if self.deadline_streak >= DEADLINE_STREAK:
            return "deadline_streak"
        waiting = [start for start in self.inflight.values() if start >= self.last_answer]
        if waiting and now - min(waiting) >= STALL_SECONDS:
            return "waiting"
        return None


@dataclass(frozen=True)
class _Suspicion:
    active: _Lane
    standby: _Lane | None
    reason: str
    stalled_ms: int
    started: float


class FailoverChannel(grpc.Channel):
    """A ``grpc.Channel`` that sends each call to the active of two channels."""

    def __init__(
        self,
        create: Callable[[], Any],
        probe: ChannelProbe,
        *,
        name: str = "primary",
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        schedule: Callable[[float, Callable[[], None]], None] = _schedule,
    ) -> None:
        self._create = create
        self._probe = probe
        self._name = name
        self._clock = clock
        self._spawn = spawn
        self._schedule = schedule
        self._lock = threading.Lock()
        now = clock()
        self._active = _Lane(create(), now)
        self._standby: _Lane | None = _Lane(create(), now)
        self._retired: list[_Lane] = []
        self._subscriptions: list[tuple[Callable[..., Any], bool]] = []
        self._next_call = 0
        self._probing = False
        self._last_probe = float("-inf")
        self._last_failover = float("-inf")
        self._failovers = 0
        self._closed = False

    # grpc.Channel

    def subscribe(self, callback: Callable[..., Any], try_to_connect: bool = False) -> None:
        # Whoever watches the channel watches the active one, across moves.
        with self._lock:
            self._subscriptions.append((callback, try_to_connect))
            self._active.channel.subscribe(callback, try_to_connect=try_to_connect)

    def unsubscribe(self, callback: Callable[..., Any]) -> None:
        with self._lock:
            self._subscriptions = [s for s in self._subscriptions if s[0] != callback]
            self._active.channel.unsubscribe(callback)

    def unary_unary(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
        _registered_method: bool = False,
    ) -> _SingleResponse:
        key = ("unary_unary", method, request_serializer, response_deserializer, _registered_method)
        return _SingleResponse(self, key)

    def unary_stream(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
        _registered_method: bool = False,
    ) -> _StreamingResponse:
        key = (
            "unary_stream",
            method,
            request_serializer,
            response_deserializer,
            _registered_method,
        )
        return _StreamingResponse(self, key)

    def stream_unary(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
        _registered_method: bool = False,
    ) -> _SingleResponse:
        key = (
            "stream_unary",
            method,
            request_serializer,
            response_deserializer,
            _registered_method,
        )
        return _SingleResponse(self, key)

    def stream_stream(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
        _registered_method: bool = False,
    ) -> _StreamingResponse:
        key = (
            "stream_stream",
            method,
            request_serializer,
            response_deserializer,
            _registered_method,
        )
        return _StreamingResponse(self, key)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            lanes = [self._active, *([self._standby] if self._standby else []), *self._retired]
            self._retired = []
        for lane in lanes:
            lane.channel.close()

    def __enter__(self) -> FailoverChannel:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # Call bookkeeping

    def _begin(self, key: _Key) -> tuple[_Lane, int, Any]:
        now = self._clock()
        with self._lock:
            suspicion = self._suspect(now)
            lane = self._active
            token = self._next_call
            self._next_call += 1
            lane.inflight[token] = now
        self._probe_later(suspicion)
        try:
            return lane, token, lane.callable(key)
        except BaseException:
            self._end(lane, token, None)
            raise

    def _end(self, lane: _Lane, token: int, status: Any) -> None:
        try:
            now = self._clock()
            with self._lock:
                lane.inflight.pop(token, None)
                if _answered(status):
                    lane.answered(now)
                elif status == grpc.StatusCode.DEADLINE_EXCEEDED:
                    lane.deadline_streak += 1
                suspicion = self._suspect(now)
            self._probe_later(suspicion)
        except Exception:  # noqa: BLE001 - bookkeeping never fails a call.
            return

    def _blocking(self, key: _Key, send: Callable[[Any], Any]) -> Any:
        lane, token, call = self._begin(key)
        try:
            result = send(call)
        except grpc.RpcError as error:
            self._end(lane, token, _status(error))
            raise
        except BaseException:
            self._end(lane, token, None)
            raise
        self._end(lane, token, grpc.StatusCode.OK)
        return result

    def _tracked(self, key: _Key, send: Callable[[Any], Any]) -> Any:
        lane, token, call = self._begin(key)
        try:
            started = send(call)
        except BaseException:
            self._end(lane, token, None)
            raise
        # Runs when the call ends however it ends, at once if it already has.
        started.add_done_callback(lambda done: self._end(lane, token, _status(done)))
        return started

    # Detection and failover

    def _suspect(self, now: float) -> _Suspicion | None:
        """Called with the lock held. Claims the next probe round, if one is due."""
        if (
            self._closed
            or self._probing
            or now - self._last_probe < PROBE_INTERVAL_SECONDS
            or now - self._last_failover < FAILOVER_INTERVAL_SECONDS
        ):
            return None
        reason = self._active.stall(now)
        if reason is None:
            return None
        self._probing = True
        self._last_probe = now
        return _Suspicion(
            active=self._active,
            standby=self._standby,
            reason=reason,
            stalled_ms=max(0, round((now - self._active.last_answer) * 1000)),
            started=now,
        )

    def _probe_later(self, suspicion: _Suspicion | None) -> None:
        if suspicion is None:
            return
        try:
            self._spawn(functools.partial(self._probe_round, suspicion))
        except Exception:  # noqa: BLE001 - no probe this round.
            with self._lock:
                self._probing = False

    def _probe_round(self, suspicion: _Suspicion) -> None:
        active = suspicion.active
        standby = suspicion.standby
        # Only when building the last replacement failed.
        built = standby is None
        try:
            if standby is None:
                standby = _Lane(self._create(), self._clock())
            standby_future = self._probe.start(standby.channel)
            active_future = self._probe.start(active.channel)
            wait = self._probe.timeout + 1.0
            standby_answered = _answered_within(standby_future, wait)
            active_answered = _answered_within(active_future, wait)
        except Exception as exc:  # noqa: BLE001 - a broken probe never moves traffic.
            with self._lock:
                self._probing = False
            if built and standby is not None:
                standby.channel.close()
            logger.warning(
                "spanner channel probe unavailable client=%s error_class=%s",
                self._name,
                type(exc).__name__,
            )
            return
        now = self._clock()
        probe_ms = max(0, round((now - suspicion.started) * 1000))
        with self._lock:
            self._probing = False
            move = (
                standby_answered
                and not active_answered
                and not self._closed
                and self._active is active
                # No real call answered on the active connection meanwhile.
                and active.last_answer <= suspicion.started
            )
            if standby_answered:
                standby.answered(now)
            if active_answered:
                active.answered(now)
            if move:
                self._active, self._standby = standby, None
                self._retired.append(active)
                self._last_failover = now
                self._failovers += 1
                for callback, try_to_connect in self._subscriptions:
                    active.channel.unsubscribe(callback)
                    standby.channel.subscribe(callback, try_to_connect=try_to_connect)
                waiting = len(active.inflight)
                streak = active.deadline_streak
                failovers = self._failovers
            elif built and self._standby is None and not self._closed:
                self._standby, built = standby, False
        if not move:
            if built:
                standby.channel.close()
            logger.info(
                "spanner channel probe kept client=%s reason=%s standby=%s active=%s "
                "stalled_ms=%d probe_ms=%d",
                self._name,
                suspicion.reason,
                "answered" if standby_answered else "silent",
                "answered" if active_answered else "silent",
                suspicion.stalled_ms,
                probe_ms,
            )
            return
        logger.warning(
            "spanner channel failover client=%s reason=%s stalled_ms=%d waiting=%d "
            "deadline_streak=%d probe_ms=%d failovers=%d",
            self._name,
            suspicion.reason,
            suspicion.stalled_ms,
            waiting,
            streak,
            probe_ms,
            failovers,
        )
        self._schedule(RETIRE_AFTER_SECONDS, functools.partial(self._retire, active))
        self._replace_standby()

    def _retire(self, lane: _Lane) -> None:
        with self._lock:
            if lane not in self._retired:
                return
            self._retired.remove(lane)
        lane.channel.close()

    def _replace_standby(self) -> None:
        try:
            lane = _Lane(self._create(), self._clock())
        except Exception as exc:  # noqa: BLE001 - the next probe round builds one.
            logger.warning(
                "spanner channel standby unavailable client=%s error_class=%s",
                self._name,
                type(exc).__name__,
            )
            return
        with self._lock:
            if self._standby is None and not self._closed:
                self._standby = lane
                return
        lane.channel.close()


class _SingleResponse(grpc.UnaryUnaryMultiCallable, grpc.StreamUnaryMultiCallable):
    """Picks the active channel each time it is called, not when it is made."""

    def __init__(self, channel: FailoverChannel, key: _Key) -> None:
        self._channel = channel
        self._key = key

    def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return self._channel._blocking(self._key, lambda call: call(request, *args, **kwargs))

    def with_call(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return self._channel._blocking(
            self._key, lambda call: call.with_call(request, *args, **kwargs)
        )

    def future(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return self._channel._tracked(self._key, lambda call: call.future(request, *args, **kwargs))


class _StreamingResponse(grpc.UnaryStreamMultiCallable, grpc.StreamStreamMultiCallable):
    """A response stream counts as answered when it ends with the server's status."""

    def __init__(self, channel: FailoverChannel, key: _Key) -> None:
        self._channel = channel
        self._key = key

    def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return self._channel._tracked(self._key, lambda call: call(request, *args, **kwargs))


def install_spanner_channel_failover(
    database: Any, *, enabled: bool = True, name: str = "primary"
) -> None:
    """Build ``database.spanner_api`` on a :class:`FailoverChannel`.

    Call before anything reads ``database.spanner_api``. The store must start
    whatever happens here, so nothing raises: when off, and on any problem,
    the SDK builds its own client on first use, as before.
    """
    if not enabled:
        logger.info("spanner channel failover off client=%s", name)
        return
    try:
        api = _failover_spanner_api(database, name)
    except Exception as exc:  # noqa: BLE001 - failover never stops startup.
        logger.warning(
            "spanner channel failover unavailable client=%s error_class=%s",
            name,
            type(exc).__name__,
        )
        return
    if api is None:
        return
    database._spanner_api = api
    logger.info("spanner channel failover armed client=%s", name)


def _failover_spanner_api(database: Any, name: str) -> Any:
    """``Database.spanner_api``, google-cloud-spanner 3.71, on a failover channel.

    The emulator and the default branches are the SDK's own, with the channel
    swapped: the transport is the SDK's class, given a channel callable that
    it calls with exactly the arguments it would pass to its own
    ``create_channel``. Each real channel is made from those arguments.
    """
    from google.api_core.gapic_v1 import routing_header
    from google.auth import credentials as ga_credentials
    from google.cloud.spanner_v1 import GetSessionRequest, Session
    from google.cloud.spanner_v1.database import SPANNER_DATA_SCOPE
    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.cloud.spanner_v1.services.spanner.transports.grpc import (
        SpannerGrpcTransport,
    )

    if database._spanner_api is not None:
        logger.warning("spanner channel failover skipped client=%s reason=built", name)
        return None
    instance = database._instance
    client = instance._client
    if getattr(client, "instance_type", None) == "omni":
        logger.info("spanner channel failover skipped client=%s reason=omni", name)
        return None
    session = f"{database.name}/sessions/{PROBE_SESSION_ID}"
    probe = ChannelProbe(
        method=PROBE_METHOD,
        request=GetSessionRequest(name=session),
        request_serializer=GetSessionRequest.serialize,
        response_deserializer=Session.deserialize,
        metadata=(
            ("google-cloud-resource-prefix", database.name),
            routing_header.to_grpc_metadata((("name", session),)),
        ),
    )
    if instance.emulator_host is not None:
        emulator = FailoverChannel(
            functools.partial(
                grpc.insecure_channel, instance.emulator_host, options=[OWN_CONNECTION]
            ),
            probe,
            name=name,
        )
        return SpannerClient(
            client_info=client._client_info,
            transport=SpannerGrpcTransport(channel=emulator),
        )

    def failover_channel(
        host: str, *, options: Sequence[Any] = (), **kwargs: Any
    ) -> FailoverChannel:
        create = functools.partial(
            SpannerGrpcTransport.create_channel,
            host,
            options=[*options, OWN_CONNECTION],
            **kwargs,
        )
        return FailoverChannel(create, probe, name=name)

    credentials = client.credentials
    if isinstance(credentials, ga_credentials.Scoped):
        credentials = credentials.with_scopes((SPANNER_DATA_SCOPE,))
    return SpannerClient(
        credentials=credentials,
        client_info=client._client_info,
        client_options=client._client_options,
        transport=functools.partial(SpannerGrpcTransport, channel=failover_channel),
    )
