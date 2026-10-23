"""Golden vectors for fastpath/internal/pyjson, the Go writer of Python's JSON bytes.

CPython computes every expected value here: float.__repr__ of doubles given
by their bits; money.microdollars_to_float's repr (the float twin money_pair
writes); json.dumps of strings with ensure_ascii on and off; for JSON
documents, storage_codec.json_body (sorted, ASCII) and Starlette's
JSONResponse body (insertion order, ensure_ascii off) of json.loads of the
text, or json.loads's refusal; and storage_gcp._iso_timestamp of instants.
The Go tests hold the package to fastpath/testdata/pyjson_vectors_v1.json;
tests/test_fastpath_pyjson_vectors.py fails when the file is not what this
script writes now.

    uv run python scripts/fastpath/pyjson_vectors.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from starlette.responses import JSONResponse  # noqa: E402

from trusted_router.money import microdollars_to_float  # noqa: E402
from trusted_router.storage_codec import json_body  # noqa: E402
from trusted_router.storage_gcp import _iso_timestamp  # noqa: E402

OUTPUT = ROOT / "fastpath" / "testdata" / "pyjson_vectors_v1.json"

# The Go decoder's nesting bound (canonical.go maxDepth).
GO_MAX_DEPTH = 256


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _around(value: float) -> list[float]:
    return [math.nextafter(value, -math.inf), value, math.nextafter(value, math.inf)]


def edge_floats() -> list[float]:
    values = [
        0.0, -0.0, 1.0, -1.0, 2.0, 10.0, 100.0, 1e15, 0.5, 1.5, -1.5, 0.1, 0.2, 0.3, 0.1 + 0.2,
        1 / 3, 2 / 3, -2 / 3, 3.141592653589793, 2.718281828459045,
        1e-05, 1.5e-05, 9.999999999999999e-05, 0.0001, 0.00015, 0.001, 0.000123456789,
        -1e-05, -0.0001,
        1e16, 9999999999999998.0, 1e17, 1.2345678901234568e16, 1234567890123456.8, 123456789012345680.0,
        -1e16, -9999999999999998.0,
        2.0**52, 2.0**53, 2.0**53 + 2, 2.0**63, 2.0**64, 2.0**1023,
        5e-324, 1e-323, 2.225073858507201e-308, 2.2250738585072014e-308, 1.7976931348623157e308,
        -5e-324, -1.7976931348623157e308,
        1e22, 1e23, 1e-07, 1e-300, 1e300, 0.30000000000000004, 0.7, 1.1, 2.675, 1e21, 1e-06, 4.35,
        0.000001, 0.000010, 0.000100, 1.0e-4 * 3, 123.456, 1000000.0, 999999.9999999999,
        float(10**15 - 1), float(10**16 - 2), 0.01, 0.07, 1.23e-04, 9.99e-05,
    ]
    for exponent in range(-8, 0):
        values += _around(10.0**exponent)
    for exponent in range(13, 19):
        values += _around(10.0**exponent)
    return values


def float_vectors() -> list[dict[str, str]]:
    values = edge_floats() + [math.nan, math.inf, -math.inf]
    rng = random.Random(17140)  # noqa: S311 - reproducible test vectors, not secrets.
    while len(values) < 1600:
        bits = rng.getrandbits(64)
        value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
        if math.isfinite(value):
            values.append(value)
    for _ in range(400):
        values.append(round(rng.uniform(-1e6, 1e6), rng.randint(0, 12)))
    for _ in range(400):
        values.append(rng.random() * 10.0 ** rng.randint(-30, 30))
    seen: set[str] = set()
    out = []
    for value in values:
        bits = _bits(value)
        if bits not in seen:
            seen.add(bits)
            out.append({"bits": bits, "repr": repr(value)})
    return out


def money_vectors() -> list[dict[str, Any]]:
    amounts = [
        0, 1, 5, 9, 10, 15, 99, 100, 101, 999, 1000, 1234, 9999, 10_000, 99_999, 100_000, 123_456,
        999_999, 1_000_000, 1_000_001, 1_500_000, 10**9, 123_456_789, 10**12, 10**15, 10**16,
        2**53, 2**53 + 1, 2**63 - 1, -1, -10, -100, -999_999, -1_000_000, -(2**63),
    ]
    rng = random.Random(26)  # noqa: S311 - reproducible test vectors, not secrets.
    for scale in (100, 10_000, 1_000_000, 10**9, 10**13):
        amounts += [rng.randint(0, scale) for _ in range(120)]
    return [{"microdollars": m, "repr": repr(microdollars_to_float(m))} for m in dict.fromkeys(amounts)]


EDGE_STRINGS = [
    "", "hello", '"', "\\", "/", "</script>", "a\"b\\c/d", "\x7f", "\x80", "\x9f", "\xa0", "\xe9", "\xff",
    "\u0100", "\u07ff", "\u0800", "\u2028", "\u2029", "\ufeff", "\ufffd", "\uffff", "\U00010000",
    "\U0001f600", "\U0010ffff", "\U0001f468\u200d\U0001f469\u200d\U0001f467", "日本語", "héllo",
    "héllo\u2028wörld\t\U0001f600\"\\", "\x00end", "tab\there", "line\r\nbreak",
    "".join(chr(c) for c in range(0x20)), "".join(chr(c) for c in range(0x20, 0x80)),
] + [chr(c) for c in range(0x21)]

STRING_ALPHABET = [
    list(range(0x20, 0x7F)), list(range(0x00, 0x20)) + [0x7F], list(range(0x80, 0x100)),
    list(range(0x100, 0xD800, 97)) + list(range(0xE000, 0x10000, 89)), list(range(0x10000, 0x110000, 4099)),
]


def random_string(rng: random.Random) -> str:
    return "".join(chr(rng.choice(rng.choice(STRING_ALPHABET))) for _ in range(rng.randint(0, 12)))


def string_vectors() -> list[dict[str, Any]]:
    rng = random.Random(2028)  # noqa: S311 - reproducible test vectors, not secrets.
    strings = EDGE_STRINGS + [random_string(rng) for _ in range(400)]
    return [
        {
            "codepoints": [ord(c) for c in s],
            "ascii": json.dumps(s, ensure_ascii=True),
            "unicode": json.dumps(s, ensure_ascii=False),
        }
        for s in dict.fromkeys(strings)
    ]


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


DOCUMENTS = [
    "{}", "[]", "null", "true", "false", "0", "-0", "-0.0", "0.0", "1", "-1", "1.0", "1e2", "1E2", "1e-2",
    "1.5E+3", "-0e0", "0e0", "1E-0", "0.1", "1e-05", "1e-5", "0.00001", "0.0001", "1e16", "10000000000000000",
    "1e16", "9999999999999998.0", "9999999999999998", "9007199254740993", "9223372036854775807",
    "9223372036854775808", "-9223372036854775808", "-9223372036854775809", "18446744073709551616",
    "123456789012345678901234567890", "1e-400", "-1e-400", "5e-324", "2.4703282292062327e-324",
    "2.4703282292062328e-324", "1.7976931348623157e308", "1.7976931348623158e308", "0.30000000000000004",
    "100000000000000000000000.0", "1" + "0" * 30 + ".5", "0." + "0" * 30 + "1", "1" * 4300, "-" + "1" * 4300,
    '"abc"', '"\\u00e9"', '"\\u00E9"', '"\\ud83d\\ude00"', '"\\uD83D\\uDE00"', '"\\u2028\\u2029"', '"\\u007f"',
    '"\\/"', '"\\b\\f\\n\\r\\t\\"\\\\"', '"\\u0000"', '"é😀\u2028\x7f"', '""',
    ' \t\n\r{"a" : 1 , "b":[1, 2 ,3]}\r\n',
    '{"b":1,"a":2,"c":{"z":null,"y":true}}',
    '{"é":1,"e":2,"z":3,"Z":4,"😀":5,"\uffff":6,"":7,"\\u0000":8}',
    '{"a":[{"c":1,"b":2}],"tags":{"k":"v","a":"\\u00e9"}}',
    '{"estimated_cost":1e-05,"estimated_cost_microdollars":10,"x":0.0001,"y":1.0}',
    '[1,[2,[3,[4,{"d":[]}]]]]', '[1.0,2.50,-0.0,0e5,1E+2,3.0e-5]',
    '{"provider":{"order":["a","b"],"allow_fallbacks":true},"max_tokens":1024,"temperature":0.7}',
    _nested(GO_MAX_DEPTH), "[" * (GO_MAX_DEPTH - 1) + "{}" + "]" * (GO_MAX_DEPTH - 1),
]

# Documents the Go decoder refuses: None where json.loads refuses them too,
# else the reason it reads them and Go does not (canonical.go's Err values).
REFUSED: list[tuple[str, str | None]] = [
    ("", None), (" ", None), ("{", None), ("[", None), ("[1,]", None), ('{"a":1,}', None), ("{'a':1}", None),
    ("01", None), ("-01", None), ("1.", None), (".5", None), ("-", None), ("+1", None), ("1e", None),
    ("1e+", None), ("1.e5", None), ("0x10", None), ("tru", None), ("nul", None), ("True", None), ("None", None),
    ('"abc', None), ('"\\x"', None), ('"\\u12"', None), ('"\\u12G4"', None), ('"\\U0041"', None),
    ('"a\nb"', None), ('"\t"', None), ('"\x00"', None), ("[1 2]", None), ('{"a" 1}', None), ("{1:2}", None),
    ("1 2", None), ("\ufeff{}", None), ("NaNa", None), ("nan", None), ("inf", None), ("-inf", None),
    ('"\\ud83d\\u12"', None), ("1" * 4301, None), ("-" + "1" * 4301, None), ("[1]]", None), ("{}}", None),
    ("\u00a0{}", None), ("\x0b1", None), ("\x0c1", None), ("[,1]", None), ('{"a":}', None), ("-a", None),
    ("NaN", "non_finite"), ("Infinity", "non_finite"), ("-Infinity", "non_finite"), ("[NaN]", "non_finite"),
    ('{"a":Infinity}', "non_finite"), ("1e400", "non_finite"), ("-1e400", "non_finite"),
    ("1" + "0" * 400 + ".0", "non_finite"), ("1.7976931348623159e308", "non_finite"),
    ('{"a":1,"a":2}', "duplicate_key"), ('[{"x":{"k":1,"k":1}}]', "duplicate_key"),
    ('{"a":1,"b":2,"a":1}', "duplicate_key"),
    ('"\\ud800"', "lone_surrogate"), ('"\\udc00"', "lone_surrogate"), ('"\\ud800\\u0041"', "lone_surrogate"),
    ('"\\ud83d"', "lone_surrogate"), ('"\\ude00\\ud83d"', "lone_surrogate"), ('"\\ud83dx"', "lone_surrogate"),
    ('"\\ud800\\ud800"', "lone_surrogate"), ('{"\\udfff":1}', "lone_surrogate"),
    (_nested(GO_MAX_DEPTH + 1), "too_deep"), ('{"a":' * (GO_MAX_DEPTH + 1) + "1" + "}" * (GO_MAX_DEPTH + 1), "too_deep"),
]


def _document(text: str) -> dict[str, str]:
    value = json.loads(text)
    return {"text": text, "canonical": json_body(value), "response": JSONResponse(value).body.decode("utf-8")}


def random_value(rng: random.Random, depth: int) -> Any:
    kind = rng.randint(0, 9 if depth < 4 else 5)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.choice([
            rng.randint(-1000, 1000), rng.randint(-(2**63), 2**63 - 1), rng.choice([2**63, -(2**63) - 1, 2**64]),
            rng.randint(-(10**40), 10**40), 0,
        ])
    if kind == 2:
        value = struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0]
        if not math.isfinite(value):
            value = 0.0
        return rng.choice([value, rng.random() * 10.0 ** rng.randint(-8, 20), float(rng.randint(-99, 99))])
    if kind in (3, 4, 5):
        return random_string(rng)
    if kind in (6, 7):
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {random_string(rng): random_value(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def document_vectors() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    documents = [_document(text) for text in dict.fromkeys(DOCUMENTS)]
    rng = random.Random(3066)  # noqa: S311 - reproducible test vectors, not secrets.
    for _ in range(250):
        value = random_value(rng, 0)
        text = json.dumps(
            value,
            ensure_ascii=rng.random() < 0.5,
            indent=rng.choice([None, None, 0, 2, "\t"]),
            separators=rng.choice([None, (",", ":"), (" , ", " : ")]),
        )
        documents.append(_document(text))
    refused = []
    for text, reason in REFUSED:
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            if reason is not None:
                raise AssertionError(f"json.loads refuses {text[:40]!r}, listed as Go's {reason}") from None
            refused.append({"text": text, "python": "refuses"})
            continue
        if reason is None:
            raise AssertionError(f"json.loads reads {text[:40]!r}, listed as refused")
        refused.append({"text": text, "python": "reads", "go_refuses": reason})
    return documents, refused


def isoformat_vectors() -> list[dict[str, Any]]:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    instants = [
        epoch, epoch + dt.timedelta(microseconds=1), epoch + dt.timedelta(microseconds=999_999),
        epoch + dt.timedelta(seconds=1), epoch - dt.timedelta(microseconds=1),
        dt.datetime(1, 1, 1, tzinfo=dt.UTC), dt.datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        dt.datetime(999, 6, 7, 8, 9, 10, 11, tzinfo=dt.UTC), dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
        dt.datetime(2024, 2, 29, 12, 0, 0, tzinfo=dt.UTC), dt.datetime(2026, 10, 23, 17, 4, 5, 100_000, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 23, 17, 4, 5, 10, tzinfo=dt.UTC),
    ]
    rng = random.Random(1448)  # noqa: S311 - reproducible test vectors, not secrets.
    low = (dt.datetime(1, 1, 1, tzinfo=dt.UTC) - epoch) // dt.timedelta(microseconds=1)
    high = (dt.datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC) - epoch) // dt.timedelta(microseconds=1)
    for _ in range(150):
        micros = rng.randint(low, high)
        if rng.random() < 0.3:
            micros -= micros % 1_000_000
        instants.append(epoch + dt.timedelta(microseconds=micros))
    return [
        {"unix_micros": (t - epoch) // dt.timedelta(microseconds=1), "isoformat": _iso_timestamp(t)} for t in instants
    ]


def render() -> str:
    """The file's text: one vector a line, so a change shows as the vectors it changes."""
    documents, refused = document_vectors()
    sections = {
        "floats": float_vectors(),
        "money": money_vectors(),
        "strings": string_vectors(),
        "documents": documents,
        "refused": refused,
        "isoformat": isoformat_vectors(),
    }
    parts = ['{"generator": "scripts/fastpath/pyjson_vectors.py"']
    for name, vectors in sections.items():
        lines = [json.dumps(v, sort_keys=True, separators=(",", ":")) for v in vectors]
        parts.append(f' "{name}": [\n  ' + ",\n  ".join(lines) + "\n ]")
    return ",\n".join(parts) + "}\n"


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
