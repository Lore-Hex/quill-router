"""Make a Spanner transport stall attributable from Cloud Run logs.

One control-plane instance can lose Spanner for a minute or more while its
siblings stay healthy: every RPC on it reaches its client deadline, and Spanner
itself counts almost none of them. Two causes fit that picture and the
application logs cannot tell them apart:

* the instance's HTTP/2 connection to the Google front end stays open but
  stops answering streams, until a keepalive ping fails and gRPC reconnects;
* the access token enters its refresh window and the refresh against the
  instance metadata server hangs. google-auth refreshes on the calling RPC's
  thread by default, so every RPC waits behind it until its deadline.

google-auth and gRPC report both through their own loggers, which reach neither
Cloud Run logs nor Axiom (``main._configure_application_logging`` attaches the
console handler to this package only). This module reports the two facts that
separate the causes under a ``trusted_router`` logger: the channel's
connectivity transitions, and how long each credential refresh took.

It also moves the refresh off the RPC thread. While a token is inside its
refresh window it is still valid, so google-auth's non-blocking mode sends the
RPC with it and refreshes on a worker thread. After a failed background
refresh google-auth falls back to refreshing inline, which is today's
behavior, so this is never worse than the default.

Only fixed words and numbers leave this module. Exceptions from the auth and
transport layers can carry tokens and endpoints; never attach one, or a stack.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

SLOW_REFRESH_SECONDS = 1.0
FAILURE_LOG_INTERVAL_SECONDS = 5.0
_WATCHED_ATTRIBUTE = "_trusted_router_transport_watch"


def watch_spanner_transport(database: Any, *, name: str = "primary") -> None:
    """Report channel transitions and credential refreshes for ``database``.

    Telemetry must not decide whether the store starts, so nothing here
    raises: a transport this cannot read is reported once and left alone.
    """
    if getattr(database, _WATCHED_ATTRIBUTE, False):
        return
    try:
        transport = database.spanner_api.transport
        _watch_channel(transport, name)
        _watch_credentials(transport, name)
        setattr(database, _WATCHED_ATTRIBUTE, True)
    except Exception as exc:  # noqa: BLE001 - telemetry never stops startup.
        logger.warning(
            "spanner transport watch unavailable client=%s error_class=%s",
            name,
            type(exc).__name__,
        )


def _watch_channel(transport: Any, name: str) -> None:
    channel = transport.grpc_channel
    # Observe only. try_to_connect=True would dial an idle channel.
    channel.subscribe(_ChannelStateLog(name).on_state, try_to_connect=False)


class _ChannelStateLog:
    """Log each connectivity state with how long the previous one lasted.

    A stall with no transition, followed by a reconnect when it ends, is the
    unresponsive-connection case. A stall with the channel READY throughout is
    not a connection problem.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._previous: str | None = None
        self._since = time.monotonic()

    def on_state(self, connectivity: Any) -> None:
        state = str(getattr(connectivity, "name", connectivity))
        now = time.monotonic()
        with self._lock:
            previous, since = self._previous, self._since
            if state == previous:
                return
            self._previous, self._since = state, now
        level = logging.WARNING if state == "TRANSIENT_FAILURE" else logging.INFO
        logger.log(
            level,
            "spanner channel state=%s previous=%s previous_ms=%d client=%s",
            state,
            previous or "none",
            0 if previous is None else max(0, round((now - since) * 1000)),
            self._name,
        )


def _watch_credentials(transport: Any, name: str) -> None:
    # The data-plane credentials are a scoped copy the SDK makes of the
    # client's (Database.spanner_api), and the copy does not inherit the
    # refresh mode. The transport holds the object the channel signs with.
    credentials = getattr(transport, "_credentials", None)
    enable = getattr(credentials, "with_non_blocking_refresh", None)
    refresh = getattr(credentials, "refresh", None)
    if credentials is None or not callable(enable) or not callable(refresh):
        # A caller-supplied channel (the emulator) carries no credentials.
        logger.info("spanner credential refresh unwatched client=%s", name)
        return
    credentials.refresh = _TimedRefresh(credentials, refresh, name)
    enable()
    logger.info(
        "spanner credential refresh watched client=%s kind=%s",
        name,
        type(credentials).__name__,
    )


class _TimedRefresh:
    """Time every refresh of one credentials object, on whichever thread."""

    def __init__(self, credentials: Any, refresh: Any, name: str) -> None:
        self._credentials = credentials
        self._refresh = refresh
        self._name = name
        self._lock = threading.Lock()
        self._next_failure_log = 0.0
        self._suppressed = 0

    def __call__(self, request: Any) -> None:
        started = time.monotonic()
        try:
            self._refresh(request)
        except Exception as exc:
            self._report_failure(type(exc).__name__, started)
            raise
        elapsed = time.monotonic() - started
        logger.log(
            logging.WARNING if elapsed >= SLOW_REFRESH_SECONDS else logging.INFO,
            "spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d client=%s",
            round(elapsed * 1000),
            _inline(),
            self._ttl_seconds(),
            self._name,
        )

    def _report_failure(self, error_class: str, started: float) -> None:
        # With the metadata server down every inline refresh fails, one per
        # RPC. Say so every few seconds and count the rest.
        now = time.monotonic()
        with self._lock:
            if now < self._next_failure_log:
                self._suppressed += 1
                return
            suppressed, self._suppressed = self._suppressed, 0
            self._next_failure_log = now + FAILURE_LOG_INTERVAL_SECONDS
        logger.warning(
            "spanner credential refresh failed error_class=%s elapsed_ms=%d "
            "inline=%s suppressed=%d client=%s",
            error_class,
            max(0, round((now - started) * 1000)),
            _inline(),
            suppressed,
            self._name,
        )

    def _ttl_seconds(self) -> int:
        expiry = getattr(self._credentials, "expiry", None)
        if expiry is None:
            return -1
        from google.auth import _helpers

        return round((expiry - _helpers.utcnow()).total_seconds())


def _inline() -> bool:
    """True when the refresh ran in an RPC's own path, where the RPC waits for it."""
    from google.auth._refresh_worker import RefreshThread

    return not isinstance(threading.current_thread(), RefreshThread)
