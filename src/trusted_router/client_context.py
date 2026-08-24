"""Bounded vocabulary and soft parsers for gateway client telemetry."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trusted_router.schemas import GatewayClientContext

CLIENT_CONTEXT_VERSIONS = (1,)
CLIENT_CONTEXT_SOURCES = ("tr", "stainless", "none")
CLIENT_SDKS = (
    "tr-py",
    "tr-js",
    "tr-go",
    "tr-rust",
    "tr-java",
    "tr-swift",
    "openai-python",
    "openai-js",
    "openai-go",
    "openai-java",
    "openai-other",
    "anthropic-python",
    "anthropic-js",
    "anthropic-go",
    "anthropic-java",
    "anthropic-other",
    "other",
)
CLIENT_LANGS = (
    "python",
    "js",
    "go",
    "rust",
    "java",
    "swift",
    "kotlin",
    "ruby",
    "csharp",
    "php",
    "dart",
    "other",
)
CLIENT_OSES = ("linux", "macos", "windows", "ios", "android", "freebsd", "other")
CLIENT_ARCHES = ("x64", "x32", "arm", "arm64", "wasm", "other")
CLIENT_PREV_OUTCOMES = (
    "none",
    "http_error",
    "transport_error",
    "timeout",
    "stream_broken",
)
CLIENT_PREV_ERROR_CLASSES = (
    "none",
    "dns",
    "tls",
    "connect_refused",
    "connect_timeout",
    "connect_error",
    "read_timeout",
    "write_timeout",
    "pool_timeout",
    "protocol_error",
    "reset",
    "io_error",
    "proxy_error",
    "stream_stalled",
    "unknown",
)
CLIENT_PREV_HOSTS = (
    "none",
    "apex",
    "ally",
    "uptime",
    "us_central1",
    "us_east4",
    "europe_west4",
    "control",
    "custom",
)

ClientContextVersion = Literal[1]
ClientContextSource = Literal["tr", "stainless", "none"]
ClientSdk = Literal[
    "tr-py",
    "tr-js",
    "tr-go",
    "tr-rust",
    "tr-java",
    "tr-swift",
    "openai-python",
    "openai-js",
    "openai-go",
    "openai-java",
    "openai-other",
    "anthropic-python",
    "anthropic-js",
    "anthropic-go",
    "anthropic-java",
    "anthropic-other",
    "other",
]
ClientLang = Literal[
    "python",
    "js",
    "go",
    "rust",
    "java",
    "swift",
    "kotlin",
    "ruby",
    "csharp",
    "php",
    "dart",
    "other",
]
ClientOs = Literal["linux", "macos", "windows", "ios", "android", "freebsd", "other"]
ClientArch = Literal["x64", "x32", "arm", "arm64", "wasm", "other"]
ClientPrevOutcome = Literal["none", "http_error", "transport_error", "timeout", "stream_broken"]
ClientPrevErrorClass = Literal[
    "none",
    "dns",
    "tls",
    "connect_refused",
    "connect_timeout",
    "connect_error",
    "read_timeout",
    "write_timeout",
    "pool_timeout",
    "protocol_error",
    "reset",
    "io_error",
    "proxy_error",
    "stream_stalled",
    "unknown",
]
ClientPrevHost = Literal[
    "none",
    "apex",
    "ally",
    "uptime",
    "us_central1",
    "us_east4",
    "europe_west4",
    "control",
    "custom",
]

_GATEWAY_REQUEST_ID_RE = re.compile(r"^rlog_[0-9a-f]{32}$")


def parse_client_context(raw: object) -> GatewayClientContext | None:
    """Return validated telemetry, dropping malformed input without raising."""
    from trusted_router.schemas import GatewayClientContext as ClientContextModel

    try:
        return ClientContextModel.model_validate(raw)
    except (TypeError, ValueError):
        return None


def parse_gateway_request_id(raw: object) -> str | None:
    """Return the bounded enclave request id or ``None`` for any invalid value."""
    if not isinstance(raw, str) or _GATEWAY_REQUEST_ID_RE.fullmatch(raw) is None:
        return None
    return raw
