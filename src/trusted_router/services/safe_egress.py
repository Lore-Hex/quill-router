from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

from trusted_router.errors import api_error
from trusted_router.types import ErrorType
from trusted_router.url_validation import parse_url

_ALLOWED_SCHEMES = ("http", "https")
# A lookup that has not answered in this long is treated as a bad host. This
# bounds how long an attacker-chosen nameserver can hold a worker thread.
RESOLVE_TIMEOUT_SECONDS = 5.0


def is_safe_public_ip(ip_str: str) -> bool:
    """Return whether an address is safe for server-side public egress."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_safe_public_ip(str(ip.ipv4_mapped))
    # `is_global` is the IANA special-purpose-registry-aware check: it is
    # False for shared address space (CGNAT 100.64.0.0/10 — never
    # internet-routable, used INSIDE clouds for internal services), which the
    # explicit predicates below miss. Keep them anyway: they are the
    # documented rule, and the enclave's Go guard enumerates the same ranges.
    return ip.is_global and not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _reject_unless_all_public(infos: list[Any]) -> None:
    if not infos:
        raise api_error(400, "URL host resolve failed", ErrorType.BAD_REQUEST)
    for _family, _, _, _, sockaddr in infos:
        if not is_safe_public_ip(str(sockaddr[0])):
            raise api_error(
                400,
                "URL host resolves to a private address",
                ErrorType.BAD_REQUEST,
            )


def resolve_public_or_reject(host: str) -> None:
    """Resolve a hostname and reject it if any answer is non-public.

    Synchronous; blocks the calling thread for the lookup. Do NOT call this
    from a coroutine — use `aresolve_public_or_reject`, or the whole event
    loop stalls on an attacker-chosen hostname's nameserver.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError) as exc:
        raise api_error(400, "URL host resolve failed", ErrorType.BAD_REQUEST) from exc
    _reject_unless_all_public(infos)


async def aresolve_public_or_reject(host: str) -> None:
    """Async twin: the lookup runs off the loop, bounded, so a slow or
    blackholed nameserver behind a registered URL cannot freeze every other
    request on the worker."""
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except (socket.gaierror, UnicodeError, TimeoutError) as exc:
        raise api_error(400, "URL host resolve failed", ErrorType.BAD_REQUEST) from exc
    _reject_unless_all_public(infos)


def validate_url_scheme(url: str) -> tuple[str, str]:
    """Return the normalized scheme and hostname for an HTTP(S) URL."""
    parsed = parse_url(url)
    if parsed is None:
        raise api_error(400, "invalid URL", ErrorType.BAD_REQUEST)
    host = parsed.hostname
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise api_error(400, "unsupported URL scheme", ErrorType.BAD_REQUEST)
    if not host:
        raise api_error(400, "invalid URL", ErrorType.BAD_REQUEST)
    return scheme, host


def _check_scheme(url: str, *, allow_http: bool) -> str:
    scheme, host = validate_url_scheme(url)
    if scheme != "https" and not allow_http:
        raise api_error(400, "URL must use https", ErrorType.BAD_REQUEST)
    return host


def assert_public_url(url: str, *, allow_http: bool) -> None:
    """Synchronous check — for non-async contexts only (see aresolve)."""
    resolve_public_or_reject(_check_scheme(url, allow_http=allow_http))


async def aassert_public_url(url: str, *, allow_http: bool) -> None:
    """The one to call from request handlers and dispatch."""
    await aresolve_public_or_reject(_check_scheme(url, allow_http=allow_http))


# ── Connect-time enforcement ────────────────────────────────────────────────
#
# The checks above answer "does this name resolve to a public address right
# now". The HTTP client then resolves the name a second time to connect, and
# nothing ties the two answers together: a nameserver the caller controls can
# give a public address to the check and an internal one to the connection.
# The clients below close that gap by doing the lookup once, inside the
# connect step, and dialing the address that was checked. TLS still names the
# original host, so certificates verify as before.

_NOT_PUBLIC = "destination address is not public"
_NO_ANSWER = "destination host did not resolve"


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def is_public_ip_literal_or_name(host: str, *, allow_loopback: bool = False) -> bool:
    """False only for an IP literal that is not a public address.

    A hostname passes: what it resolves to is decided at connect time, by the
    clients below, which is the only moment the answer can be trusted.
    """
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return True
    if is_safe_public_ip(literal):
        return True
    return allow_loopback and _is_loopback(literal)


def _dial_addresses(infos: list[Any], *, allow_loopback: bool) -> list[str]:
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise httpcore.ConnectError(_NO_ANSWER)
    for address in addresses:
        if is_safe_public_ip(address):
            continue
        if allow_loopback and _is_loopback(address):
            continue
        # One internal answer condemns the whole name, as in
        # `_reject_unless_all_public`: a name that mixes public and internal
        # answers is not one we want to be steered by.
        raise httpcore.ConnectError(_NOT_PUBLIC)
    return addresses


class _PublicOnlyBackend(httpcore.NetworkBackend):
    def __init__(self, *, allow_loopback: bool) -> None:
        self._inner = httpcore.SyncBackend()
        self._allow_loopback = allow_loopback

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise httpcore.ConnectError(_NO_ANSWER) from exc
        # Each checked address is tried in the resolver's order, as a plain
        # connect by name would, so a host with an unreachable first answer
        # still connects.
        failure: Exception = httpcore.ConnectError(_NO_ANSWER)
        for address in _dial_addresses(infos, allow_loopback=self._allow_loopback):
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError(_NOT_PUBLIC)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _PublicOnlyAsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, allow_loopback: bool) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._allow_loopback = allow_loopback

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        budget = RESOLVE_TIMEOUT_SECONDS if timeout is None else min(timeout, RESOLVE_TIMEOUT_SECONDS)
        try:
            infos = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(_NO_ANSWER) from exc
        except (socket.gaierror, UnicodeError) as exc:
            raise httpcore.ConnectError(_NO_ANSWER) from exc
        failure: Exception = httpcore.ConnectError(_NO_ANSWER)
        for address in _dial_addresses(infos, allow_loopback=self._allow_loopback):
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError(_NOT_PUBLIC)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_egress_client(
    *, timeout: float | httpx.Timeout, allow_loopback: bool = False
) -> httpx.Client:
    """An HTTP client for URLs a customer chose.

    It connects only to public addresses, checked at the moment of connecting,
    and never follows a redirect: a redirect is the other way a public URL
    turns into a request to something internal. Environment proxy settings
    are ignored, since a proxy would do the connecting in our place.

    `allow_loopback` is for local development and tests, where the collector
    under test listens on this machine.
    """
    transport = httpx.HTTPTransport(trust_env=False)
    # httpx builds the pool itself and does not take a network backend, so
    # the pool is replaced with one that does. The first pool has opened
    # nothing. `test_safe_egress_clients.py` fails if a library upgrade moves
    # this attribute, and proves the refusal against a real listener.
    transport._pool = httpcore.ConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        network_backend=_PublicOnlyBackend(allow_loopback=allow_loopback),
    )
    return httpx.Client(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    )


def public_egress_async_client(
    *, timeout: float | httpx.Timeout, allow_loopback: bool = False
) -> httpx.AsyncClient:
    """Async twin of `public_egress_client`; the lookup runs off the loop."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        network_backend=_PublicOnlyAsyncBackend(allow_loopback=allow_loopback),
    )
    return httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
    )
