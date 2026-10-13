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

import re
from _json import scanstring
from collections.abc import Callable, Iterator
from typing import Any

from trusted_router.async_settle_shadow_v2_a1_deltas import broadening
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical

# CPython JSON accepts ASCII digits only, including in fractions/exponents.
NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")


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


def _number_tokens(raw: bytes, paths: tuple[tuple[str, ...], ...]) -> Iterator[tuple[str, bool]]:
    """Label the syntactically scanned prefix, without enforcing transport policy.

    Use the decoder's string scanner and number grammar (including partial
    numbers such as 1 in 1e+). An explicit stack keeps planning nonrecursive.
    Constants are values, but never number-hook tokens. Duplicate members keep
    their original positions. A malformed suffix cannot erase earlier labels.
    This is only a plan: the authoritative decoder owns every refusal.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return
    index = 0
    pending = [("value", paths)]
    while pending:
        state, allowed = pending.pop()
        while index < len(text) and text[index] in " \t\n\r":
            index += 1
        if index == len(text):
            return
        char = text[index]
        if state in ("object-first", "object-key"):
            if char == "}" and state == "object-first":
                index += 1
                continue
            if char != '"':
                return
            try:
                key, index = scanstring(text, index + 1)
            except ValueError:
                return
            while index < len(text) and text[index] in " \t\n\r":
                index += 1
            if index == len(text) or text[index] != ":":
                return
            index += 1
            pending.extend((("object-next", allowed),
                            ("value", tuple(p[1:] for p in allowed if p and p[0] == key))))
        elif state in ("object-next", "array-next"):
            if char == ("}" if state == "object-next" else "]"):
                index += 1
            elif char == ",":
                index += 1
                if state == "object-next":
                    pending.append(("object-key", allowed))
                else:
                    pending.extend((("array-next", allowed), ("value", allowed)))
            else:
                return
        elif state == "array-first":
            if char == "]":
                index += 1
            else:
                pending.extend((("array-next", allowed), ("value", allowed)))
        elif char == "{":
            index += 1
            pending.append(("object-first", allowed))
        elif char == "[":
            index += 1
            pending.append(("array-first", tuple(p[1:] for p in allowed if p and p[0] == "*")))
        elif char == '"':
            try:
                _, index = scanstring(text, index + 1)
            except ValueError:
                return
        else:
            match = NUMBER_RE.match(text, index)
            if match is not None:
                yield match.group(), () in allowed
                index = match.end()
            else:
                constant = next((v for v in ("null", "true", "false", "NaN", "Infinity", "-Infinity")
                                 if text.startswith(v, index)), None)
                if constant is None:
                    return
                index += len(constant)


def number_decoder(raw: bytes, paths: tuple[tuple[str, ...], ...]) -> Callable[[str, Callable[[str], Any]], Any]:
    """Apply prefix path labels to the authoritative decoder's number calls.

    Planning is lazy: only the next numeric path is requested at each hook call.
    No successful whole-document parse is needed, and later syntax cannot
    change an earlier selection. Schemas without paths never call this planner.
    """
    tokens = _number_tokens(raw, paths)

    def parse(lexeme: str, retained: Callable[[str], Any]) -> Any:
        _, allowed = next(tokens)
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
