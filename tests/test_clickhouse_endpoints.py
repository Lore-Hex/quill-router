"""ClickHouse read failover across an ordered endpoint list.

The single-endpoint tests pin today's behavior (every deployment configures
one URL): one attempt, the caller's timeout for every phase, every error
raised. The multi-endpoint tests pin when a read may move to the next replica
and, as importantly, when it must not.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from trusted_router.clickhouse_endpoints import (
    FAILOVER_CONNECT_TIMEOUT_SECONDS,
    parse_endpoints,
    request_timeout,
)
from trusted_router.config import operational_analytics_sink_problems
from trusted_router.operational_analytics import (
    PUBLIC_SNAPSHOT_QUERY_TIMEOUT_SECONDS,
    OperationalAnalyticsClient,
)
from trusted_router.provider_analytics import ProviderAnalyticsClient

LB = "http://lb.internal:8123"
REPLICA_2 = "http://replica-2.internal:8123"
REPLICA_3 = "http://replica-3.internal:8123"
OK_ROWS = {"data": [{"value": 1}]}


def _client(base_url: str, handler) -> OperationalAnalyticsClient:
    return OperationalAnalyticsClient(
        base_url=base_url,
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


def _router(behaviour: dict[str, object], seen: list[httpx.Request]):
    """Answer per host: an exception class to raise, a status code, or OK rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = behaviour.get(request.url.host, 200)
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("simulated", request=request)
        assert isinstance(outcome, int)
        if outcome == 200:
            return httpx.Response(200, json=OK_ROWS)
        return httpx.Response(outcome, text="unavailable")

    return handler


def _hosts(seen: list[httpx.Request]) -> list[str]:
    return [request.url.host for request in seen]


# --------------------------------------------------------------------- parsing


def test_parse_endpoints_keeps_order_and_drops_blanks_slashes_and_duplicates() -> None:
    assert parse_endpoints("http://ch:8123/") == ("http://ch:8123",)
    assert parse_endpoints(f" {LB}/ , {REPLICA_2},,{LB} ,{REPLICA_3}/") == (
        LB,
        REPLICA_2,
        REPLICA_3,
    )
    assert parse_endpoints("") == ()
    assert parse_endpoints(" , ") == ()


def test_an_empty_endpoint_list_is_refused_like_an_empty_url() -> None:
    with pytest.raises(ValueError, match="URL is required"):
        OperationalAnalyticsClient(base_url=" , ", user="reader", password="secret")  # noqa: S106
    with pytest.raises(ValueError, match="URL is required"):
        ProviderAnalyticsClient(base_url="", user="reader", password="secret")  # noqa: S106


def test_request_timeout_bounds_connect_only_when_there_is_a_fallback() -> None:
    single = request_timeout((LB,), 20.0)
    assert (single.connect, single.read, single.write, single.pool) == (20.0, 20.0, 20.0, 20.0)

    failover = request_timeout((LB, REPLICA_2), 20.0)
    assert FAILOVER_CONNECT_TIMEOUT_SECONDS == 1.0
    assert failover.connect == 1.0
    assert (failover.read, failover.write, failover.pool) == (20.0, 20.0, 20.0)

    # A budget below the connect bound is never stretched.
    short = request_timeout((LB, REPLICA_2), 0.5)
    assert short.connect == 0.5


# ------------------------------------------------------ one endpoint: unchanged


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
def test_single_endpoint_raises_connect_failures_after_one_attempt(failure) -> None:
    seen: list[httpx.Request] = []
    client = _client(LB, _router({"lb.internal": failure}, seen))

    with pytest.raises(failure):
        client._query("SELECT 1 FORMAT JSON")

    assert _hosts(seen) == ["lb.internal"]


def test_single_endpoint_raises_a_503_after_one_attempt() -> None:
    seen: list[httpx.Request] = []
    client = _client(LB, _router({"lb.internal": 503}, seen))

    with pytest.raises(httpx.HTTPStatusError):
        client._query("SELECT 1 FORMAT JSON")

    assert _hosts(seen) == ["lb.internal"]


def test_single_endpoint_keeps_the_callers_timeout_for_every_phase() -> None:
    seen: list[httpx.Request] = []
    client = _client(LB, _router({}, seen))

    assert client._query("SELECT 1 FORMAT JSON") == [{"value": 1}]

    timeout = seen[0].extensions["timeout"]
    assert timeout == {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}


# ------------------------------------------------- several endpoints: failover


@pytest.mark.parametrize(
    "first",
    [httpx.ConnectError, httpx.ConnectTimeout, 502, 503, 504],
)
def test_unreachable_or_unavailable_endpoint_fails_over_to_the_next(first, caplog) -> None:
    seen: list[httpx.Request] = []
    client = _client(f"{LB},{REPLICA_2}", _router({"lb.internal": first}, seen))

    with caplog.at_level(logging.WARNING, logger="trusted_router.clickhouse_endpoints"):
        assert client._query("SELECT 1 FORMAT JSON") == [{"value": 1}]

    assert _hosts(seen) == ["lb.internal", "replica-2.internal"]
    # The same query, parameters and credentials reach the replica.
    assert seen[1].content == seen[0].content
    assert seen[1].url.params == seen[0].url.params
    assert seen[1].headers["authorization"] == seen[0].headers["authorization"]
    assert "clickhouse.endpoint_failover" in caplog.text
    assert "from_host=lb.internal" in caplog.text


@pytest.mark.parametrize(
    "first",
    [httpx.ReadTimeout, httpx.RemoteProtocolError, 400, 401, 403, 404, 500],
)
def test_errors_that_every_replica_shares_are_raised_without_failover(first) -> None:
    seen: list[httpx.Request] = []
    client = _client(f"{LB},{REPLICA_2}", _router({"lb.internal": first}, seen))

    expected = first if isinstance(first, type) else httpx.HTTPStatusError
    with pytest.raises(expected):
        client._query("SELECT 1 FORMAT JSON")

    # A query that may still be running, or that fails the same way on every
    # replica, is never sent a second time.
    assert _hosts(seen) == ["lb.internal"]


def test_every_endpoint_is_tried_once_in_order_then_the_last_error_is_raised() -> None:
    seen: list[httpx.Request] = []
    behaviour = {
        "lb.internal": httpx.ConnectError,
        "replica-2.internal": 503,
        "replica-3.internal": httpx.ConnectTimeout,
    }
    client = _client(f"{LB},{REPLICA_2},{REPLICA_3}", _router(behaviour, seen))

    with pytest.raises(httpx.ConnectTimeout):
        client._query("SELECT 1 FORMAT JSON")

    assert _hosts(seen) == ["lb.internal", "replica-2.internal", "replica-3.internal"]


def test_the_last_endpoints_503_is_raised_not_swallowed() -> None:
    seen: list[httpx.Request] = []
    behaviour = {"lb.internal": httpx.ConnectError, "replica-2.internal": 503}
    client = _client(f"{LB},{REPLICA_2}", _router(behaviour, seen))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        client._query("SELECT 1 FORMAT JSON")

    assert raised.value.response.status_code == 503
    assert _hosts(seen) == ["lb.internal", "replica-2.internal"]


def test_a_healthy_first_endpoint_is_the_only_one_contacted() -> None:
    seen: list[httpx.Request] = []
    client = _client(f"{LB},{REPLICA_2},{REPLICA_3}", _router({}, seen))

    assert client._query("SELECT 1 FORMAT JSON") == [{"value": 1}]

    assert _hosts(seen) == ["lb.internal"]


def test_failover_bounds_connect_but_keeps_the_public_snapshot_read_budget() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"payload": '{"generated_at": "x"}'}]})

    client = _client(f"{LB},{REPLICA_2}", handler)

    assert client.public_snapshot("leaderboard") == {"generated_at": "x"}

    timeout = seen[0].extensions["timeout"]
    assert PUBLIC_SNAPSHOT_QUERY_TIMEOUT_SECONDS == 2.0
    assert timeout == {"connect": 1.0, "read": 2.0, "write": 2.0, "pool": 2.0}


# ------------------------------------------------------- async provider client


def _provider_client(base_url: str, handler) -> ProviderAnalyticsClient:
    return ProviderAnalyticsClient(
        base_url=base_url,
        user="reader",
        password="secret",  # noqa: S106 - test-only fake transport credential
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.anyio
async def test_provider_queries_fail_over_on_connect_failure() -> None:
    seen: list[httpx.Request] = []
    client = _provider_client(
        f"{LB},{REPLICA_2}", _router({"lb.internal": httpx.ConnectError}, seen)
    )

    rows = await client._json_query("SELECT 1 FORMAT JSON", provider="neurometric", days=7)

    assert rows == [{"value": 1}]
    assert _hosts(seen) == ["lb.internal", "replica-2.internal"]
    assert seen[1].url.params["param_provider"] == "neurometric"


@pytest.mark.anyio
async def test_provider_single_endpoint_raises_after_one_attempt() -> None:
    seen: list[httpx.Request] = []
    client = _provider_client(LB, _router({"lb.internal": httpx.ConnectError}, seen))

    with pytest.raises(httpx.ConnectError):
        await client._json_query("SELECT 1 FORMAT JSON", provider="neurometric", days=7)

    assert _hosts(seen) == ["lb.internal"]


@pytest.mark.anyio
async def test_provider_query_500_is_not_retried() -> None:
    seen: list[httpx.Request] = []
    client = _provider_client(f"{LB},{REPLICA_2}", _router({"lb.internal": 500}, seen))

    with pytest.raises(httpx.HTTPStatusError):
        await client._json_query("SELECT 1 FORMAT JSON", provider="neurometric", days=7)

    assert _hosts(seen) == ["lb.internal"]


@pytest.mark.anyio
async def test_csv_export_fails_over_before_streaming_and_closes_the_refused_response() -> None:
    seen: list[httpx.Request] = []
    closed: list[str] = []

    class _TrackedStream(httpx.ByteStream):
        def __init__(self, body: bytes, host: str) -> None:
            super().__init__(body)
            self._host = host

        async def aclose(self) -> None:
            closed.append(self._host)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = request.url.host
        if host == "lb.internal":
            return httpx.Response(503, stream=_TrackedStream(b"busy", host))
        return httpx.Response(
            200, stream=_TrackedStream(b"request_metadata_id\nid-1\n", host)
        )

    client = _provider_client(f"{LB},{REPLICA_2}", handler)
    export = await client.open_csv_export("neurometric", days=7)
    payload = b"".join([chunk async for chunk in export.chunks()])

    assert payload == b"request_metadata_id\nid-1\n"
    assert _hosts(seen) == ["lb.internal", "replica-2.internal"]
    assert closed == ["lb.internal", "replica-2.internal"]


# ------------------------------------------------------ direct sink stays single


def _sink_settings(**overrides) -> SimpleNamespace:
    base = dict(
        operational_analytics_sink="outbox",
        operational_analytics_outbox_enabled=True,
        operational_analytics_clickhouse_url=f"{LB},{REPLICA_2}",
        operational_analytics_clickhouse_write_password="pw",  # noqa: S106
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_endpoint_list_is_accepted_for_the_outbox_sink() -> None:
    assert operational_analytics_sink_problems(_sink_settings()) == []


def test_endpoint_list_is_refused_for_the_direct_sink() -> None:
    problems = " ".join(
        operational_analytics_sink_problems(
            _sink_settings(operational_analytics_sink="direct")
        )
    )
    assert "comma-separated endpoint list" in problems


def test_single_url_direct_sink_is_unchanged() -> None:
    assert (
        operational_analytics_sink_problems(
            _sink_settings(
                operational_analytics_sink="direct",
                operational_analytics_clickhouse_url=LB,
            )
        )
        == []
    )
