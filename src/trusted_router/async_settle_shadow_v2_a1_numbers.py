"""Opt-in binary64 transport; baseline codecs keep their integer-only domain."""
from __future__ import annotations

import json
import re
from typing import Any

from trusted_router.async_settle_shadow_v2_a1_deltas import broadening
from trusted_router.async_settle_shadow_v2_deltas import apply_deltas
from trusted_router.async_settle_shadow_v2_schema import _bad_number, _number, _pairs, require
from trusted_router.detached_jws import canonical


def binary64(value: Any) -> None:
    require(broadening("a1-binary64-wall", value))


def _float(lexeme: str) -> float:
    value = float(lexeme)
    binary64(value)
    require(canonical(value).decode("ascii") == lexeme, "binary64_spelling")
    return value


def _int(lexeme: str) -> int:
    require(len(lexeme) <= 310 and re.fullmatch(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)", lexeme) is not None, "integer")
    return int(lexeme)


def decode_numbers(raw: bytes, limit: int, paths: tuple[tuple[str, ...], ...]) -> Any:
    require(type(raw) is bytes and len(raw) <= limit, "evidence_size")
    require(not raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")), "json_encoding")
    require(all(c < 128 for c in raw), "json_encoding")
    try:
        apply_deltas("retained-transport", raw)
        value = json.loads(raw, object_pairs_hook=_pairs, parse_int=_int,
                           parse_float=_float, parse_constant=_bad_number)
        apply_deltas("every v2 object", value, binary64_paths=paths)
        apply_deltas("retained-object", value)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("json_encoding") from exc
    except ValueError as exc:
        if str(exc) == "transport-spelling":
            raise ValueError("json_encoding") from exc
        raise

    def numbers(child: Any, allowed: tuple[tuple[str, ...], ...]) -> None:
        if type(child) is dict:
            for key, member in child.items():
                numbers(member, tuple(p[1:] for p in allowed if p and p[0] == key))
        elif type(child) is list:
            for member in child:
                numbers(member, tuple(p[1:] for p in allowed if p and p[0] == "*"))
        elif type(child) in (int, float):
            if () in allowed:
                binary64(child)
            else:
                require(type(child) is int, "integer")
                _number(str(child))
    numbers(value, paths)
    return value


def age_microseconds(age_seconds: Any) -> int:
    """Ceil the exact binary64 subtraction result, without rounded multiplication."""
    binary64(age_seconds)
    numerator, denominator = float(age_seconds).as_integer_ratio()
    return (numerator * 1000000 + denominator - 1) // denominator
