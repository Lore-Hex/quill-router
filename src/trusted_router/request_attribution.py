"""Validation for OpenRouter-compatible request attribution metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

MAX_USER_CHARS = 256
MAX_SESSION_CHARS = 256
MAX_APP_CHARS = 120
MAX_REFERER_CHARS = 2048
MAX_CATEGORIES = 2
MAX_CATEGORY_CHARS = 30
MAX_TRACE_UTF8_BYTES = 8192
MAX_TRACE_DEPTH = 8
MAX_TRACE_ITEMS = 256
_CATEGORY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InvalidAttribution(ValueError):
    """A request attribution error safe to expose to an API client."""


@dataclass(frozen=True)
class RequestAttribution:
    user: str | None
    session_id: str | None
    trace: dict[str, Any] | None
    app: str | None
    http_referer: str | None
    app_categories: list[str]

    def body_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.user is not None:
            fields["user"] = self.user
        if self.session_id is not None:
            fields["session_id"] = self.session_id
        if self.trace is not None:
            fields["trace"] = self.trace
        if self.app is not None:
            fields["app"] = self.app
        if self.http_referer is not None:
            fields["http_referer"] = self.http_referer
        if self.app_categories:
            fields["app_categories"] = list(self.app_categories)
        return fields


def validate_request_attribution(
    *,
    user: str | None,
    session_id: str | None,
    trace: dict[str, Any] | None,
    app: str | None,
    http_referer: str | None,
    app_categories: list[str] | None,
) -> RequestAttribution:
    user = _bounded_string(user, "user", MAX_USER_CHARS)
    session_id = _bounded_string(session_id, "session_id", MAX_SESSION_CHARS)
    app = _bounded_string(app, "app title", MAX_APP_CHARS)
    http_referer = _valid_referer(http_referer)
    categories = _valid_categories(app_categories)
    trace = _valid_trace(trace)
    if app is None and http_referer is not None:
        app = urlparse(http_referer).hostname
    return RequestAttribution(
        user=user,
        session_id=session_id,
        trace=trace,
        app=app,
        http_referer=http_referer,
        app_categories=categories,
    )


def _bounded_string(value: str | None, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) > limit:
        raise InvalidAttribution(f"{field} may contain at most {limit} characters")
    return value


def _valid_referer(value: str | None) -> str | None:
    value = _bounded_string(value, "HTTP-Referer", MAX_REFERER_CHARS)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidAttribution("HTTP-Referer must be an http or https URL")
    return value


def _valid_categories(values: list[str] | None) -> list[str]:
    values = list(values or [])
    if len(values) > MAX_CATEGORIES:
        raise InvalidAttribution(f"app categories may contain at most {MAX_CATEGORIES} values")
    for value in values:
        if len(value) > MAX_CATEGORY_CHARS or not _CATEGORY.fullmatch(value):
            raise InvalidAttribution(
                "app categories must be lowercase kebab-case with at most 30 characters"
            )
    return values


def _valid_trace(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise InvalidAttribution("trace must contain valid JSON values") from exc
    if len(encoded) > MAX_TRACE_UTF8_BYTES:
        raise InvalidAttribution("trace must use at most 8192 UTF-8 bytes")
    count = _trace_item_count(value, depth=1)
    if count > MAX_TRACE_ITEMS:
        raise InvalidAttribution("trace may contain at most 256 keys and array elements")
    return value


def _trace_item_count(value: Any, *, depth: int) -> int:
    if depth > MAX_TRACE_DEPTH:
        raise InvalidAttribution("trace may be at most 8 levels deep")
    if isinstance(value, dict):
        return len(value) + sum(
            _trace_item_count(item, depth=depth + 1) for item in value.values()
        )
    if isinstance(value, list):
        return len(value) + sum(
            _trace_item_count(item, depth=depth + 1) for item in value
        )
    return 0
