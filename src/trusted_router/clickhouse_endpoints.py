"""Ordered ClickHouse HTTP endpoints, with failover, for the read clients.

A ClickHouse URL setting names one endpoint or an ordered, comma-separated
list of them, for example the private load balancer followed by the replicas
behind it. Every GCP replica holds the whole dataset (one shard, three
ReplicatedMergeTree replicas; docs/design/clickhouse-high-availability.md), so
any of them can answer any read.

One endpoint, which is what every deployment configures today, behaves exactly
as before this module existed: one attempt, the caller's timeout, every error
raised. Failover is only possible when the setting lists somewhere else to go.

The client moves to the next endpoint only when the query provably never
reached ClickHouse (``httpx.ConnectError`` or ``httpx.ConnectTimeout``) or when
the endpoint answered 502, 503 or 504. A read timeout, a 500 or any 4xx is
raised: those fail identically on every replica, or would run an expensive
query a second time while the first may still be executing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

FAILOVER_STATUS_CODES = frozenset({502, 503, 504})
# Connecting to a private VPC address takes milliseconds. With a fallback
# configured, a dead endpoint should cost about one second, not the whole query
# budget. The single-endpoint form keeps the caller's timeout for every phase.
FAILOVER_CONNECT_TIMEOUT_SECONDS = 1.0

_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)

log = logging.getLogger(__name__)


def parse_endpoints(value: str) -> tuple[str, ...]:
    """Return the configured endpoints in order, without trailing slashes or duplicates."""
    stripped = (part.strip().rstrip("/") for part in value.split(","))
    return tuple(dict.fromkeys(endpoint for endpoint in stripped if endpoint))


def request_timeout(endpoints: tuple[str, ...], seconds: float) -> httpx.Timeout:
    """The caller's timeout; with a fallback configured, connects are bounded separately."""
    if len(endpoints) < 2:
        return httpx.Timeout(seconds)
    return httpx.Timeout(seconds, connect=min(seconds, FAILOVER_CONNECT_TIMEOUT_SECONDS))


def _log_failover(endpoints: tuple[str, ...], index: int, reason: str) -> None:
    log.warning(
        "clickhouse.endpoint_failover from_index=%d from_host=%s to_index=%d reason=%s",
        index,
        httpx.URL(endpoints[index]).host,
        index + 1,
        reason,
    )


def send_with_failover(
    client: httpx.Client,
    endpoints: tuple[str, ...],
    build: Callable[[str], httpx.Request],
) -> httpx.Response:
    """Send ``build(endpoint)`` to each endpoint in order until one can serve it."""
    last = len(endpoints) - 1
    for index, endpoint in enumerate(endpoints):
        try:
            response = client.send(build(endpoint))
        except _CONNECT_FAILURES as exc:
            if index == last:
                raise
            _log_failover(endpoints, index, type(exc).__name__)
            continue
        if index < last and response.status_code in FAILOVER_STATUS_CODES:
            response.close()
            _log_failover(endpoints, index, f"http_{response.status_code}")
            continue
        return response
    raise ValueError("no ClickHouse endpoint is configured")


async def send_with_failover_async(
    client: httpx.AsyncClient,
    endpoints: tuple[str, ...],
    build: Callable[[str], httpx.Request],
    *,
    stream: bool = False,
) -> httpx.Response:
    """Async ``send_with_failover``; a streamed response fails over only before its body."""
    last = len(endpoints) - 1
    for index, endpoint in enumerate(endpoints):
        try:
            response = await client.send(build(endpoint), stream=stream)
        except _CONNECT_FAILURES as exc:
            if index == last:
                raise
            _log_failover(endpoints, index, type(exc).__name__)
            continue
        if index < last and response.status_code in FAILOVER_STATUS_CODES:
            await response.aclose()
            _log_failover(endpoints, index, f"http_{response.status_code}")
            continue
        return response
    raise ValueError("no ClickHouse endpoint is configured")
