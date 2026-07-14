"""AWS-style request tag validation and deterministic merging."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any

MAX_TAGS = 50
MAX_TAG_KEY_CHARS = 128
MAX_TAG_VALUE_CHARS = 256
MAX_TAGS_UTF8_BYTES = 4096
RESERVED_TAG_PREFIXES = ("aws:", "trustedrouter:")
_PORTABLE_PUNCTUATION = frozenset("+-=._:/@")


class InvalidTags(ValueError):
    """A stable validation failure safe to return to an API client."""


def validate_tags(value: Any, *, field_name: str = "tags") -> dict[str, str]:
    """Return a detached canonical tag map or raise :class:`InvalidTags`.

    The limits intentionally mirror common AWS resource-tag semantics: at
    most 50 case-sensitive string pairs, 128-character keys, 256-character
    values, and the portable cross-service character set.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidTags(f"{field_name} must be an object")
    if len(value) > MAX_TAGS:
        raise InvalidTags(f"{field_name} may contain at most {MAX_TAGS} entries")

    result: dict[str, str] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str):
            raise InvalidTags("tag key must be a string")
        if not 1 <= len(key) <= MAX_TAG_KEY_CHARS:
            raise InvalidTags(
                f"tag key must contain 1 to {MAX_TAG_KEY_CHARS} characters"
            )
        if key.casefold().startswith(RESERVED_TAG_PREFIXES):
            raise InvalidTags("tag key uses a reserved prefix")
        if not _portable_tag_text(key):
            raise InvalidTags("tag key contains unsupported characters")
        if not isinstance(raw_value, str):
            raise InvalidTags("tag value must be a string")
        if len(raw_value) > MAX_TAG_VALUE_CHARS:
            raise InvalidTags(
                f"tag value must contain at most {MAX_TAG_VALUE_CHARS} characters"
            )
        if not _portable_tag_text(raw_value):
            raise InvalidTags("tag value contains unsupported characters")
        result[key] = raw_value
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_TAGS_UTF8_BYTES:
        raise InvalidTags(
            f"{field_name} must use at most {MAX_TAGS_UTF8_BYTES} UTF-8 bytes"
        )
    return result


def merge_tags(defaults: Any, request_tags: Any) -> dict[str, str]:
    """Overlay request tags on API-key defaults and validate the result.

    API-key defaults are validated when written; the effective map is still
    validated after merging to enforce aggregate limits and catch corruption.
    """
    supplied = validate_tags(request_tags)
    # Non-dict stored defaults can only mean row corruption (writes validate);
    # treat them as absent rather than 500ing the authorize path.
    merged = dict(defaults) if isinstance(defaults, dict) else {}
    merged.update(supplied)
    try:
        return validate_tags(merged, field_name="effective tags")
    except InvalidTags as exc:
        raise InvalidTags(
            f"effective tags are invalid after merging key defaults "
            f"({len(defaults or {})}) and request tags ({len(supplied)}): {exc}"
        ) from exc


def tags_match(given: Any, frozen: Mapping[str, str]) -> bool:
    """Compare a supplied settle map with authorization-frozen tags."""
    return validate_tags(given) == dict(frozen)


def _portable_tag_text(value: str) -> bool:
    for char in value:
        if char in _PORTABLE_PUNCTUATION:
            continue
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or category == "Zs":
            continue
        return False
    return True
