"""The Spanner transport watch: what it reports and what it changes.

The credentials here are fakes built on google-auth's own base classes, so
the refresh decisions under test (when a token is refreshed, on which thread,
and what happens after a failed attempt) are the library's, not a model of
them. Nothing in this file reaches a network: no RPC is sent, the channel is
never asked to connect, and no real credentials object is ever refreshed.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from google.auth import _helpers, compute_engine
from google.auth import credentials as ga_credentials

from trusted_router import storage_gcp_transport_watch as watch

LOGGER = "trusted_router.storage_gcp_transport_watch"
URL = "https://spanner.googleapis.com/google.spanner.v1.Spanner/Commit"
PRIVATE_TEXT = "ya29.private-token from http://metadata.private"


class _RefreshControl:
    """One refresh behavior, shared by a credentials object and its copies."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.error: Exception | None = None
        self.count = 0


class _Credentials(ga_credentials.Scoped, ga_credentials.Credentials):
    """Scoped like the metadata-server credentials: a scoped copy is a new object."""

    def __init__(self, control: _RefreshControl, scopes: Any = None) -> None:
        super().__init__()
        self._control = control
        self._scopes = scopes

    @property
    def requires_scopes(self) -> bool:
        return False

    def with_scopes(self, scopes: Any, default_scopes: Any = None) -> _Credentials:
        return type(self)(self._control, scopes)

    def refresh(self, request: Any) -> None:
        control = self._control
        control.entered.set()
        if not control.release.wait(10):
            raise AssertionError("the test never released the refresh")
        if control.error is not None:
            raise control.error
        control.count += 1
        self.token = f"token-{control.count}"
        self.expiry = _helpers.utcnow() + datetime.timedelta(hours=1)


class _Channel:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[Any, bool]] = []

    def subscribe(self, callback: Any, try_to_connect: bool = False) -> None:
        self.subscriptions.append((callback, try_to_connect))


def _database(credentials: Any, channel: Any = None) -> Any:
    transport = SimpleNamespace(grpc_channel=channel or _Channel())
    if credentials is not None:
        transport._credentials = credentials
    return SimpleNamespace(spanner_api=SimpleNamespace(transport=transport))


def _into_refresh_window(credentials: Any) -> None:
    """A token that is still valid, and due for refresh."""
    credentials.token = "token-old"  # noqa: S105
    credentials.expiry = _helpers.utcnow() + datetime.timedelta(minutes=2)


def _authorization(credentials: Any) -> str:
    headers: dict[str, str] = {}
    credentials.before_request(object(), "POST", URL, headers)
    return headers["authorization"]


def _messages(caplog: Any) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _wait_for_message(caplog: Any, prefix: str) -> str:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for message in _messages(caplog):
            if message.startswith(prefix):
                return message
        time.sleep(0.01)
    raise AssertionError(f"no log line starting {prefix!r}: {_messages(caplog)}")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: Any) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(watch, "time", fake)
    return fake


def test_default_refresh_holds_the_request_behind_it() -> None:
    """The behavior the watch changes, shown without it."""
    control = _RefreshControl()
    credentials = _Credentials(control)
    _into_refresh_window(credentials)
    control.release.clear()
    done = threading.Event()
    seen: list[str] = []

    def call() -> None:
        seen.append(_authorization(credentials))
        done.set()

    threading.Thread(target=call, daemon=True).start()

    assert control.entered.wait(5)
    assert not done.wait(0.2)
    control.release.set()
    assert done.wait(5)
    assert seen == ["Bearer token-1"]


def test_watched_request_does_not_wait_for_the_refresh(caplog: Any) -> None:
    control = _RefreshControl()
    credentials = _Credentials(control)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(_database(credentials))
        _into_refresh_window(credentials)
        control.release.clear()

        # Returns with the token it has while the refresh is still held.
        assert _authorization(credentials) == "Bearer token-old"
        assert control.entered.wait(5)
        assert control.count == 0

        control.release.set()
        refreshed = _wait_for_message(caplog, "spanner credential refresh ok")

    assert " inline=False " in refreshed
    assert " ttl_s=3600 " in refreshed or " ttl_s=3599 " in refreshed
    assert refreshed.endswith(" client=primary")
    assert _authorization(credentials) == "Bearer token-1"
    assert control.count == 1
    assert "spanner credential refresh watched client=primary kind=_Credentials" in _messages(
        caplog
    )


def test_first_token_is_fetched_inline_and_said_so(caplog: Any) -> None:
    credentials = _Credentials(_RefreshControl())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(_database(credentials), name="other")
        assert _authorization(credentials) == "Bearer token-1"

    line = _wait_for_message(caplog, "spanner credential refresh ok")
    assert " inline=True " in line
    assert line.endswith(" client=other")


def test_failed_background_refresh_falls_back_to_inline(caplog: Any) -> None:
    """google-auth's own fallback: after one failed attempt, today's behavior."""
    control = _RefreshControl()
    control.error = RuntimeError(PRIVATE_TEXT)
    credentials = _Credentials(control)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(_database(credentials))
        _into_refresh_window(credentials)

        assert _authorization(credentials) == "Bearer token-old"
        failed = _wait_for_message(caplog, "spanner credential refresh failed")
        credentials._refresh_worker._worker.join(5)
        with pytest.raises(RuntimeError):
            _authorization(credentials)

    assert " error_class=RuntimeError " in failed
    assert " inline=False " in failed
    assert " suppressed=0 " in failed
    ours = [record for record in caplog.records if record.name == LOGGER]
    assert all(record.exc_info is None and record.stack_info is None for record in ours)
    assert all("private" not in record.getMessage() for record in ours)


def test_refresh_failures_are_reported_every_few_seconds_with_a_count(
    caplog: Any, clock: _Clock
) -> None:
    def refresh(_request: Any) -> None:
        clock.now += 30.0
        raise TimeoutError(PRIVATE_TEXT)

    timed = watch._TimedRefresh(SimpleNamespace(expiry=None), refresh, "primary")

    def fail() -> None:
        with pytest.raises(TimeoutError):
            timed(object())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        fail()
        clock.now -= 29.0  # two more that ended inside the interval
        fail()
        clock.now -= 30.0
        fail()
        clock.now += watch.FAILURE_LOG_INTERVAL_SECONDS
        fail()

    assert _messages(caplog) == [
        "spanner credential refresh failed error_class=TimeoutError elapsed_ms=30000 "
        "inline=True suppressed=0 client=primary",
        "spanner credential refresh failed error_class=TimeoutError elapsed_ms=30000 "
        "inline=True suppressed=2 client=primary",
    ]
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert "private" not in caplog.text


def test_slow_refresh_is_a_warning_and_a_quick_one_is_not(caplog: Any, clock: _Clock) -> None:
    took = [0.25, watch.SLOW_REFRESH_SECONDS]

    def refresh(_request: Any) -> None:
        clock.now += took.pop(0)

    timed = watch._TimedRefresh(SimpleNamespace(expiry=None), refresh, "primary")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        timed(object())
        timed(object())

    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (
            logging.INFO,
            "spanner credential refresh ok elapsed_ms=250 inline=True ttl_s=-1 client=primary",
        ),
        (
            logging.WARNING,
            "spanner credential refresh ok elapsed_ms=1000 inline=True ttl_s=-1 client=primary",
        ),
    ]


def test_channel_transitions_are_logged_with_the_time_in_the_previous_state(
    caplog: Any, clock: _Clock
) -> None:
    channel = _Channel()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(_database(None, channel))
        ((on_state, try_to_connect),) = channel.subscriptions
        # Observing must not dial an idle channel.
        assert try_to_connect is False

        on_state(grpc.ChannelConnectivity.IDLE)
        clock.now += 0.5
        on_state(grpc.ChannelConnectivity.CONNECTING)
        clock.now += 0.02
        on_state(grpc.ChannelConnectivity.READY)
        clock.now += 2100.0
        on_state(grpc.ChannelConnectivity.READY)  # repeats are not transitions
        clock.now += 20.0
        on_state(grpc.ChannelConnectivity.TRANSIENT_FAILURE)

    states = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("spanner channel")
    ]
    assert states == [
        (logging.INFO, "spanner channel state=IDLE previous=none previous_ms=0 client=primary"),
        (
            logging.INFO,
            "spanner channel state=CONNECTING previous=IDLE previous_ms=500 client=primary",
        ),
        (
            logging.INFO,
            "spanner channel state=READY previous=CONNECTING previous_ms=20 client=primary",
        ),
        (
            logging.WARNING,
            "spanner channel state=TRANSIENT_FAILURE previous=READY previous_ms=2120000 "
            "client=primary",
        ),
    ]


def test_transport_without_credentials_still_has_its_channel_watched(caplog: Any) -> None:
    """The emulator's transport is built on a caller's channel, with no credentials."""
    channel = _Channel()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(_database(None, channel))

    assert len(channel.subscriptions) == 1
    assert _messages(caplog) == ["spanner credential refresh unwatched client=primary"]


def test_watching_twice_wraps_and_subscribes_once() -> None:
    channel = _Channel()
    credentials = _Credentials(_RefreshControl())
    database = _database(credentials, channel)

    watch.watch_spanner_transport(database)
    wrapped = credentials.refresh
    watch.watch_spanner_transport(database)

    assert credentials.refresh is wrapped
    assert len(channel.subscriptions) == 1


def test_unreadable_transport_is_reported_and_never_raises(caplog: Any) -> None:
    class Unreadable:
        @property
        def spanner_api(self) -> Any:
            raise RuntimeError(PRIVATE_TEXT)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(Unreadable())
        watch.watch_spanner_transport(object())

    assert _messages(caplog) == [
        "spanner transport watch unavailable client=primary error_class=RuntimeError",
        "spanner transport watch unavailable client=primary error_class=AttributeError",
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert "private" not in caplog.text


@pytest.fixture
def real_database(monkeypatch: Any) -> Any:
    """A real SDK database on real gRPC, signed by whatever credentials it is given."""
    from google.cloud import spanner

    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    transports: list[Any] = []

    def build(credentials: Any) -> Any:
        client = spanner.Client(
            project="project", credentials=credentials, disable_builtin_metrics=True
        )
        database = client.instance("instance").database("database")
        transports.append(database.spanner_api.transport)
        return database

    yield build
    for transport in transports:
        transport.close()


def test_watch_reaches_the_credentials_the_sdk_channel_signs_with(
    real_database: Any, caplog: Any
) -> None:
    """The SDK signs with a scoped copy of the client's credentials.

    Setting the refresh mode on the object handed to ``spanner.Client`` would
    change nothing, which is why the watch works on the transport.
    """
    control = _RefreshControl()
    given = _Credentials(control)
    database = real_database(given)
    signing = database.spanner_api.transport._credentials
    assert signing is not given

    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.watch_spanner_transport(database)
        _into_refresh_window(signing)
        control.release.clear()
        assert _authorization(signing) == "Bearer token-old"
        assert control.entered.wait(5)
        control.release.set()
        _wait_for_message(caplog, "spanner credential refresh ok")
        # A real channel reports where it is as soon as it is observed, and
        # observing it does not make it connect.
        idle = _wait_for_message(caplog, "spanner channel state=")

    assert idle == "spanner channel state=IDLE previous=none previous_ms=0 client=primary"
    assert control.count == 1


def test_watch_sets_non_blocking_refresh_on_metadata_server_credentials(
    real_database: Any,
) -> None:
    """Production's credential class, through the SDK's own copy. Never refreshed."""
    database = real_database(compute_engine.Credentials(universe_domain="googleapis.com"))
    signing = database.spanner_api.transport._credentials
    assert isinstance(signing, compute_engine.Credentials)
    assert signing._use_non_blocking_refresh is False

    watch.watch_spanner_transport(database)

    assert signing._use_non_blocking_refresh is True
    assert isinstance(signing.refresh, watch._TimedRefresh)
