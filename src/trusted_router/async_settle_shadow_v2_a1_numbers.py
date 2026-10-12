"""Opt-in binary64 numbers; transport policy belongs to the shared decoder.

Canonical float lexemes are exactly detached_jws.canonical / CPython JSON's
shortest round-trip binary64 spelling. Both 1 (native int) and 1.0 (native float)
are legal and remain distinct; -0.0 preserves the floating zero sign, while -0
is not a canonical integer. Nonnegative finite binary64 values include the
smallest subnormal 5e-324 and largest finite 1.7976931348623157e+308.
Fixed notation includes 0.0001 through values below 1e16; 1e-05 and 1e+16 use
lowercase e, an explicit exponent sign, and at least two exponent digits (e-07,
e+20). Redundant zeros (1.00), uppercase E, missing plus signs (1e20), and
alternate exponent padding (1e-7, 1e-007) are not canonical. Integer lexemes
use decimal digits without exponents, leading zeros or a plus sign. Exact
binary64-representable integers beyond int64 remain legal on declared paths.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from trusted_router.async_settle_shadow_v2_a1_deltas import broadening
from trusted_router.async_settle_shadow_v2_schema import require
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


def number_decoder(raw: bytes, paths: tuple[tuple[str, ...], ...]) -> Callable[[str, Callable[[str], Any]], Any] | None:
    """Plan path selection only; the shared decoder owns all transport policy.

    A permissive structural pass labels number tokens in source order. The real
    decoder then invokes either the declared binary64 parser or its original
    number parser on the ORIGINAL bytes. Thus even diagnostic precedence for
    undeclared numbers, duplicate keys and malformed transport stays retained.
    Failed structural planning falls back to that original decoder's diagnostic.
    """
    class Members(list[tuple[str, Any]]):
        pass

    tokens: list[object] = []

    def token(lexeme: str) -> object:
        marker = object()
        tokens.append(marker)
        return marker

    try:
        value = json.loads(raw, object_pairs_hook=Members, parse_int=token, parse_float=token)
    except (ValueError, RecursionError, UnicodeError):
        return None
    selected: set[int] = set()

    def numbers(child: Any, allowed: tuple[tuple[str, ...], ...]) -> None:
        if not allowed:
            return
        if type(child) is Members:
            for key, member in child:
                numbers(member, tuple(p[1:] for p in allowed if p and p[0] == key))
        elif type(child) is list:
            for member in child:
                numbers(member, tuple(p[1:] for p in allowed if p and p[0] == "*"))
        elif () in allowed:
            selected.add(id(child))
    numbers(value, paths)
    if not selected:
        return None
    index = 0

    def parse(lexeme: str, retained: Callable[[str], Any]) -> Any:
        nonlocal index
        allowed = id(tokens[index]) in selected
        index += 1
        if not allowed:
            return retained(lexeme)
        result = _float(lexeme) if any(c in lexeme for c in ".eE") else _int(lexeme)
        binary64(result)
        return result
    return parse


def age_microseconds(age_seconds: Any) -> int:
    """Ceil the exact binary64 subtraction result, without rounded multiplication."""
    binary64(age_seconds)
    numerator, denominator = float(age_seconds).as_integer_ratio()
    return (numerator * 1000000 + denominator - 1) // denominator
