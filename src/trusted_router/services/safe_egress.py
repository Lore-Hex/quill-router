from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from trusted_router.errors import api_error
from trusted_router.types import ErrorType

_ALLOWED_SCHEMES = ("http", "https")


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


def resolve_public_or_reject(host: str) -> None:
    """Resolve a hostname and reject it if any answer is non-public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError) as exc:
        raise api_error(400, "URL host resolve failed", ErrorType.BAD_REQUEST) from exc
    if not infos:
        raise api_error(400, "URL host resolve failed", ErrorType.BAD_REQUEST)
    for _family, _, _, _, sockaddr in infos:
        if not is_safe_public_ip(str(sockaddr[0])):
            raise api_error(
                400,
                "URL host resolves to a private address",
                ErrorType.BAD_REQUEST,
            )


def validate_url_scheme(url: str) -> tuple[str, str]:
    """Return the normalized scheme and hostname for an HTTP(S) URL."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise api_error(400, "unsupported URL scheme", ErrorType.BAD_REQUEST)
    host = parsed.hostname
    if not host:
        raise api_error(400, "invalid URL", ErrorType.BAD_REQUEST)
    return scheme, host


def assert_public_url(url: str, *, allow_http: bool) -> None:
    scheme, host = validate_url_scheme(url)
    if scheme != "https" and not allow_http:
        raise api_error(400, "URL must use https", ErrorType.BAD_REQUEST)
    resolve_public_or_reject(host)
