"""Strict Ed25519 compact JWS primitives with caller-owned type and purpose.

Canonical serialization preserves JSON scalar types. Verification accepts the
restricted ASCII JSON wire domain: no escapes, floats, duplicates, or integers
outside signed int64. Payload canonicality and claim bindings belong to callers.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_INT = (1 << 63) - 1


class JWSError(ValueError):
    """Content-free refusal reason for invalid compact JWS input."""


@dataclass(frozen=True)
class TrustedKey:
    kid: str
    purpose: str
    public_key_b64url: str
    iss: str = ""
    aud: str = ""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise JWSError(reason)


def _integer(value: Any) -> None:
    _require(type(value) is int and 0 <= value <= MAX_INT, "integer")


def _string(value: Any) -> None:
    _require(isinstance(value, str) and bool(value) and all(
        0x20 <= ord(c) <= 0x7E and c not in '\\"<>&' for c in value
    ), "string")


def _object(value: Any, strings: str = "", integers: str = "", nested: str = "") -> None:
    _require(isinstance(value, dict) and set(value) == set(
        (strings + " " + integers + " " + nested).split()), "fields")
    for field in strings.split():
        _string(value[field])
    for field in integers.split():
        _integer(value[field])


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(pairs: list[tuple[str, Any]], duplicates: list[bool]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        duplicates.append(key in result)
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise JWSError("integer")


def _parse_int(value: str) -> int:
    # Bound BEFORE int(): independent of sys.set_int_max_str_digits().
    digits = value.removeprefix("-")
    _require(len(digits) <= 19, "integer")
    number = int(value)
    _integer(number)
    return number


def _depth(text: str) -> None:
    depth = 0
    quoted = False
    for char in text:
        if quoted:
            if char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            _require(depth <= 16, "json")
        elif char in "]}":
            depth -= 1


def _json_bytes(raw: bytes) -> None:
    _require(all(0x20 <= byte <= 0x7E and byte != 0x5C for byte in raw), "json")


def _json(raw: bytes) -> Any:
    _json_bytes(raw)
    text = raw.decode("ascii")
    _depth(text)
    try:
        # Validate the WHOLE syntax without converting numbers or raising semantic
        # errors in hooks. Record duplicates, but defer their refusal until EOF.
        duplicates: list[bool] = []
        json.loads(text, object_pairs_hook=lambda pairs: _pairs(pairs, duplicates),
                   parse_int=str, parse_float=str, parse_constant=str)
        _require(not any(duplicates), "duplicate_key")
        # Syntax and duplicates are settled; bounded numeric validation may now fail.
        return json.loads(text, parse_int=_parse_int,
                          parse_float=_constant, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise JWSError("json") from exc


def b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64decode(value: str) -> bytes:
    _require(bool(value) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None, "base64")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise JWSError("base64") from exc
    _require(b64encode(raw) == value, "base64")
    return raw


def verify(token: str, keys: Sequence[TrustedKey], typ: str,
            purpose: str) -> tuple[dict[str, Any], TrustedKey, bytes]:
    _require(isinstance(token, str) and len(token) <= 65536 and token.count(".") == 2,
             "compact")
    h, p, s = token.split(".")
    header_bytes = b64decode(h)
    header = _json(header_bytes)
    _object(header, "alg kid typ")
    _require(header_bytes == canonical(header), "canonical_header")
    _require(header["alg"] == "EdDSA", "algorithm")
    _require(header["typ"] == typ, "type")
    matches = [key for key in keys if key.kid == header["kid"]]
    _require(len(matches) == 1, "key")
    key = matches[0]
    _require(key.purpose == purpose, "purpose")
    payload = b64decode(p)
    _json_bytes(payload)
    signature = b64decode(s)
    try:
        Ed25519PublicKey.from_public_bytes(b64decode(key.public_key_b64url)).verify(
            signature, (h + "." + p).encode("ascii"))
    except (ValueError, InvalidSignature) as exc:
        raise JWSError("signature") from exc
    claims = _json(payload)
    _require(isinstance(claims, dict), "fields")
    return claims, key, payload
