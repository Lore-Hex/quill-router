"""Machine-readable upstream error detail carried on a stored probe sample.

A failed synthetic probe used to keep only our error class, the HTTP status and
the provider's human message. Sentry QUILL-ROUTER-J1 (a 400 that said only
"That request was rejected") could not name the rejected field without a
credentialed call, although the attested gateway already forwards the
provider's ``error.type``, ``error.code`` and ``error.param`` and stamps every
response with its own ``x-request-id``.

Those values ride in a short prefix on the stored ``error_message``::

    {type=invalid_request_error code=unsupported_parameter param=temperature request_id=abc} message

so the ClickHouse, Spanner and Postgres sample schemas and the probe ingest
contract stay unchanged. Every value is a plain identifier-shaped string with a
bounded length; anything else is dropped, never escaped. Request content, keys
and headers other than a request ID are never read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_models import scrub_provider_error_message

# Same bound the probe write path and the internal ingest apply.
ERROR_MESSAGE_MAX_CHARS = 300
# The prefix never takes more than this, so a stored message keeps room for the
# provider's own words.
_PREFIX_MAX_CHARS = 240
# Prefix order, which is also the priority when the prefix budget runs out.
_FIELD_MAX_CHARS = {
    "type": 64,
    "code": 64,
    "param": 64,
    "request_id": 96,
    "provider_request_id": 96,
}
_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\[\]-]*")
_PREFIX_RE = re.compile(r"\{([^{}\s=]+=[^{}\s=]+(?: [^{}\s=]+=[^{}\s=]+)*)\}(?: |$)")
# The gateway synthesizes this type when the provider sent none; it says nothing.
_UNINFORMATIVE_TYPES = frozenset({"provider_error", "error"})
_REQUEST_ID_HEADERS = ("x-request-id", "request-id")


@dataclass(frozen=True)
class ProviderErrorDetail:
    type: str | None = None
    code: str | None = None
    param: str | None = None
    # The gateway's own request ID: correlates the attempt with gateway logs.
    request_id: str | None = None
    # The provider's request ID, when the gateway forwards one.
    provider_request_id: str | None = None

    def fields(self) -> dict[str, str]:
        values = {
            "type": self.type,
            "code": self.code,
            "param": self.param,
            "request_id": self.request_id,
            "provider_request_id": self.provider_request_id,
        }
        return {name: value for name, value in values.items() if value}


def _clean(name: str, value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 0 < len(value) <= _FIELD_MAX_CHARS[name] or not _VALUE_RE.fullmatch(value):
        return None
    # A key-shaped value is identifier-shaped too; drop it rather than store it.
    if scrub_provider_error_message(value) != value:
        return None
    return value


def detail_from_error(
    error: Mapping[str, Any] | None,
    headers: Mapping[str, str] | None = None,
    *,
    status: int | None = None,
) -> ProviderErrorDetail:
    """Read the bounded machine-readable fields of an OpenAI-style error object."""
    error = error if isinstance(error, Mapping) else {}
    raw_type = error.get("type")
    error_type = _clean("type", raw_type)
    if error_type is not None and error_type.casefold() in _UNINFORMATIVE_TYPES:
        error_type = None
    raw_code = error.get("code")
    # A numeric code equal to the HTTP status repeats what the sample stores.
    if isinstance(raw_code, int) and not isinstance(raw_code, bool) and raw_code == status:
        raw_code = None
    metadata = error.get("metadata")
    provider_request_id = (
        _clean("provider_request_id", metadata.get("provider_request_id"))
        if isinstance(metadata, Mapping)
        else None
    )
    request_id = None
    for header in _REQUEST_ID_HEADERS:
        request_id = _clean("request_id", (headers or {}).get(header))
        if request_id is not None:
            break
    return ProviderErrorDetail(
        type=error_type,
        code=_clean("code", raw_code),
        param=_clean("param", error.get("param")),
        request_id=request_id,
        provider_request_id=provider_request_id,
    )


def format_error_message(
    message: str | None, detail: ProviderErrorDetail | None = None,
) -> str | None:
    """Scrub and bound a message, prefixed with the detail fields it carries."""
    body = scrub_provider_error_message(message or "")
    parts: list[str] = []
    used = 2  # braces
    for name, value in (detail.fields() if detail else {}).items():
        part = f"{name}={value}"
        cost = len(part) + (1 if parts else 0)
        if used + cost > _PREFIX_MAX_CHARS:
            continue
        parts.append(part)
        used += cost
    if not parts:
        return body[:ERROR_MESSAGE_MAX_CHARS] or None
    prefix = "{" + " ".join(parts) + "}"
    if not body:
        return prefix
    return f"{prefix} {body}"[:ERROR_MESSAGE_MAX_CHARS]


def parse_error_message(message: str | None) -> tuple[ProviderErrorDetail, str | None]:
    """Split a stored message into its detail prefix and the provider's words.

    A message without a valid prefix (every pre-existing sample) comes back
    unchanged with an empty detail.
    """
    if not message:
        return ProviderErrorDetail(), message
    match = _PREFIX_RE.match(message)
    if match is None:
        return ProviderErrorDetail(), message
    values: dict[str, str] = {}
    for pair in match.group(1).split(" "):
        name, _, value = pair.partition("=")
        if name not in _FIELD_MAX_CHARS or name in values:
            return ProviderErrorDetail(), message
        cleaned = _clean(name, value)
        if cleaned is None:
            return ProviderErrorDetail(), message
        values[name] = cleaned
    rest = message[match.end():] or None
    return ProviderErrorDetail(**values), rest
