"""Pure exact-shape primitives for the dormant shadow-v2 byte contract.

The native-json-domain delta first checks every original v2 object for exact
JSON-native types and at most sixteen container levels, before any delegation.
Retained JSON-object constraint inventory (sample/counter standalone v1 object
validators; wire complete v1 raw decoder plus validator): exact retained keys,
field types, booleans and integer ranges are delegated to their closed v1
schemas. New keys/types/nesting belong to the
new-field schemas. Duplicate keys, UTF-8/BOM/raw-control validity and numeric
lexemes are already rejected by the legacy wire decoder; they have no analogue
in an already decoded object. Wrapper byte budgets are the three size deltas.
Canonical ASCII-escaped spelling of ALL raw string tokens (keys included) is
transport-spelling; surrogate exclusion on retained object strings is
scalar-string. Escaped NUL/control characters, noncharacters and other Unicode
scalars are not additionally banned here. Exact v2 tags and invariants involving
new fields are new-field checks. The oracle must never reuse this decoder to
validate retained fields: see the independently executed references and raw
paired transports in test_async_settle_shadow_v2_evidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from trusted_router.async_settle_shadow_wire import bounded_json
from trusted_router.detached_jws import canonical

POLICY = "shadow-v2"
INT64 = (1 << 63) - 1
UINT64 = (1 << 64) - 1
Check = Callable[[Any], None]


def require(condition: bool, reason: str = "evidence_schema_gap") -> None:
    if not condition:
        raise ValueError(reason)


def integer(value: Any) -> None:
    require(type(value) is int and 0 <= value <= INT64)


def signed_integer(value: Any) -> None:
    require(type(value) is int and -INT64 <= value <= INT64)


def boolean(value: Any) -> None:
    require(type(value) is bool)


def literal(*choices: Any) -> Check:
    def check(value: Any) -> None:
        require(any(type(value) is type(c) and value == c for c in choices))
    return check


def text(limit: int, pattern: str = r"[A-Za-z0-9_./:@+\-]+") -> Check:
    def check(value: Any) -> None:
        require(type(value) is str and value.isascii() and 1 <= len(value) <= limit
                and re.fullmatch(pattern, value) is not None)
    return check


native_identity = text(64)
# Boot, authorization locator and invocation nonce have the strict domain.
identity = text(64, r"[A-Za-z0-9_-]+")


def ascii_text(limit: int, minimum: int = 0) -> Check:
    def check(value: Any) -> None:
        require(type(value) is str and value.isascii() and minimum <= len(value) <= limit)
    return check


def bounded_string(limit: int, minimum: int = 1) -> Check:
    def check(value: Any) -> None:
        require(type(value) is str and minimum <= len(value) <= limit)
    return check


def string(value: Any) -> None:
    require(type(value) is str)

name = text(128)
digest = text(64, r"[0-9a-f]{64}")
revision = text(40, r"[0-9a-f]{40}")


def day(value: Any) -> None:
    text(10, r"[0-9]{4}-[0-9]{2}-[0-9]{2}")(value)
    require(dt.date.fromisoformat(value).isoformat() == value)


def day_at(microseconds: int) -> str:
    try:
        return (dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=microseconds)).date().isoformat()
    except OverflowError as exc:
        raise ValueError("evidence_schema_gap") from exc


def sequence(value: Any) -> None:
    text(20, r"[0-9]{20}")(value)
    require(1 <= int(value) <= UINT64)


def nullable(check: Check) -> Check:
    def validate(value: Any) -> None:
        if value is not None:
            check(value)
    return validate


def array(check: Check, maximum: int, minimum: int = 0, *, unique: bool = False) -> Check:
    def validate(value: Any) -> None:
        require(type(value) is list and minimum <= len(value) <= maximum)
        for child in value:
            check(child)
        if unique:
            require(len({canonical(child) for child in value}) == len(value))
    return validate


def mapping(key: Check, child: Check, maximum: int) -> Check:
    def validate(value: Any) -> None:
        require(type(value) is dict and len(value) <= maximum)
        for k, v in value.items():
            key(k)
            child(v)
    return validate


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate_key")
        result[key] = value
    return result


def _number(lexeme: str) -> int:
    require(re.fullmatch(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)", lexeme) is not None
            and len(lexeme.removeprefix("-")) <= 19, "integer")
    result = int(lexeme)
    signed_integer(result)
    return result


def _bad_number(lexeme: str) -> Any:
    raise ValueError("integer")


def decode(raw: bytes, limit: int, *, retained_strings: bool = False, binary64_paths: tuple[tuple[str, ...], ...] = ()) -> Any:
    """Strict ASCII by default; retained mode admits canonical scalar strings.

    Whitespace is allowed for transport; byte limits charge those actual bytes.
    Signed objects separately require canonical bytes. Fixture final LF is outside
    the object and must be removed explicitly by fixture readers.
    """
    require(type(raw) is bytes and len(raw) <= limit, "evidence_size")
    number_hook = None
    if binary64_paths:
        from trusted_router.async_settle_shadow_v2_a1_numbers import number_decoder
    if retained_strings:
        # V1's pure decoder preserves scalar Unicode/escaped ASCII values,
        # bounds depth and integers, and rejects duplicate keys and surrogates.
        try:
            # Local import avoids the schema/delta definition cycle. The one
            # executable table owns the disclosed retained transport policy.
            from trusted_router.async_settle_shadow_v2_deltas import apply_deltas
            apply_deltas("retained-transport", raw)
            if binary64_paths:
                number_hook = number_decoder(raw, binary64_paths)
            if number_hook is None:
                return bounded_json(raw, limit, signed=True)
            return bounded_json(raw, limit, signed=True, number_hook=number_hook)
        except ValueError as exc:
            raise ValueError({"json_duplicate": "duplicate_key", "transport-spelling": "json_encoding"}.get(str(exc), str(exc))) from exc
    require(all(32 <= c <= 126 and c != 92 for c in raw), "json_encoding")
    depth = 0
    quoted = False
    for char in raw:
        if char == 34:
            quoted = not quoted
        elif not quoted:
            if char in (91, 123):
                depth += 1
                require(depth <= 16, "json_depth")
            elif char in (93, 125):
                depth -= 1
    if binary64_paths:
        number_hook = number_decoder(raw, binary64_paths)
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_int=_number if number_hook is None else lambda token: number_hook(token, _number),
                          parse_float=_bad_number if number_hook is None else lambda token: number_hook(token, _bad_number),
                          parse_constant=_bad_number)
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise ValueError("json_shape") from exc


@dataclass(frozen=True)
class Schema:
    fields: Mapping[str, Check]
    limit: int
    invariant: Check | None = None
    retained_strings: bool = False
    binary64_paths: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def __call__(self, value: Any) -> None:
        # Inspect the original native object before any subclass delegation or
        # canonicalization can erase container/scalar types or join surrogates.
        from trusted_router.async_settle_shadow_v2_deltas import apply_deltas
        apply_deltas("every v2 object", value, binary64_paths=self.binary64_paths)
        if self.retained_strings:
            apply_deltas("retained-object", value)
        self._validate(value)

    def _validate(self, value: Any) -> None:
        require(type(value) is dict and set(value) == set(self.fields))
        raw = canonical(value)
        self._decode(raw)
        for field, check in self.fields.items():
            check(value[field])
        if self.invariant is not None:
            self.invariant(value)

    def _decode(self, raw: bytes) -> Any:
        if self.binary64_paths:
            return decode(raw, self.limit, retained_strings=self.retained_strings,
                          binary64_paths=self.binary64_paths)
        return decode(raw, self.limit, retained_strings=self.retained_strings)

    def parse(self, raw: bytes) -> dict[str, Any]:
        value = self._decode(raw)
        self(value)
        assert isinstance(value, dict)
        return value

    def serialize(self, value: dict[str, Any]) -> bytes:
        self(value)
        return canonical(value)


def versioned(fields: Mapping[str, Check], limit: int, invariant: Check | None = None, *, retained_strings: bool = False) -> Schema:
    return Schema({"v": literal(2), "policy_version": literal(POLICY), **fields}, limit, invariant, retained_strings)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


COORDINATES = Schema({"day": day, "boot": identity, "first_seq": sequence,
                      "event_seq": sequence}, 164,
                     lambda v: require(v["first_seq"] <= v["event_seq"]))
LOCATOR = Schema({**COORDINATES.fields, "event_sha256": digest}, 246,
                 lambda v: require(v["first_seq"] <= v["event_seq"]))


def receipt_key(locator: dict[str, Any]) -> tuple[str, str]:
    LOCATOR(locator)
    key = "/".join(locator[k] for k in ("day", "boot", "first_seq"))
    require(len(key) <= 96, "original_receipt_locator_gap")
    return "async_settle_shadow_receipts_v2", key
