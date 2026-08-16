from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from trusted_router.errors import api_error
from trusted_router.types import ErrorType

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
    return not (
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
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as exc:
        # urlparse raises on malformed IPv6 brackets ("host]:1@127.0.0.1");
        # that is a bad URL, not a server error.
        raise api_error(400, "invalid URL", ErrorType.BAD_REQUEST) from exc
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
