"""Golden vectors for fastpath/internal/pyjson, the Go writer of Python's JSON bytes.

CPython computes every expected value here: float.__repr__ of doubles given
by their bits; money.microdollars_to_float's repr (the float twin money_pair
writes); json.dumps of strings with ensure_ascii on and off; for JSON
documents, storage_codec.json_body (sorted, ASCII) and Starlette's
JSONResponse body (insertion order, ensure_ascii off) of json.loads of the
text, or json.loads's refusal; and storage_gcp._iso_timestamp of instants.

The pydantic_* sections are the authorize route's 200 body: FastAPI writes a
route with a response model (authorize's is its return annotation,
dict[str, Any]) and the default response class through pydantic-core's
dump_json, not json.dumps. Each is the body a request reads back from an
endpoint declared with the real route's response settings that returns the
value: of floats, of money_pair amounts, of strings, and of
{"data": json.loads(text)} for documents.
The Go tests hold the package to fastpath/testdata/pyjson_vectors_v1.json;
tests/test_fastpath_pyjson_vectors.py fails when the file is not what this
script writes now.

    uv run python scripts/fastpath/pyjson_vectors.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Nothing here may depend on the shell that runs it (CI shards set TR_*
# lifecycle overrides): the settings the imports read start from defaults.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from trusted_router.money import microdollars_to_float, money_pair  # noqa: E402
from trusted_router.routes.internal import register_gateway_internal_routes  # noqa: E402
from trusted_router.storage_codec import json_body  # noqa: E402
from trusted_router.storage_gcp import _iso_timestamp  # noqa: E402

OUTPUT = ROOT / "fastpath" / "testdata" / "pyjson_vectors_v1.json"

# The Go decoder's nesting bound (canonical.go maxDepth).
GO_MAX_DEPTH = 256


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _pow10(exponent: int) -> float:
    """10**exponent, correctly rounded on every platform.

    float.__pow__ is the C library's pow, which is not correctly rounded
    everywhere (glibc 2.36 gives 10.0**23 = 1.0000000000000001e+23, macOS
    1e+23), so the vectors would differ by machine. Python's decimal reader
    is exact everywhere.
    """
    return float(f"1e{exponent}")


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
        float(2**52), float(2**53), float(2**53) + 2, float(2**63), float(2**64), float(2**1023),
        5e-324, 1e-323, 2.225073858507201e-308, 2.2250738585072014e-308, 1.7976931348623157e308,
        -5e-324, -1.7976931348623157e308,
        1e22, 1e23, 1e-07, 1e-300, 1e300, 0.30000000000000004, 0.7, 1.1, 2.675, 1e21, 1e-06, 4.35,
        0.000001, 0.000010, 0.000100, 1.0e-4 * 3, 123.456, 1000000.0, 999999.9999999999,
        float(10**15 - 1), float(10**16 - 2), 0.01, 0.07, 1.23e-04, 9.99e-05,
    ]
    for exponent in range(-8, 0):
        values += _around(_pow10(exponent))
    for exponent in range(13, 19):
        values += _around(_pow10(exponent))
    return values


def float_values() -> list[float]:
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
        values.append(rng.random() * _pow10(rng.randint(-30, 30)))
    # Where pydantic-core's unpadded exponent gains a digit.
    for power in (1e-10, 1e-100, 1e100, 1e-99, 1e99):
        values += _around(power)
    seen: set[str] = set()
    out = []
    for value in values:
        key = _bits(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def float_vectors() -> list[dict[str, str]]:
    return [{"bits": _bits(value), "repr": repr(value)} for value in float_values()]


def money_amounts() -> list[int]:
    amounts = [
        0, 1, 5, 9, 10, 15, 99, 100, 101, 999, 1000, 1234, 9999, 10_000, 99_999, 100_000, 123_456,
        999_999, 1_000_000, 1_000_001, 1_500_000, 10**9, 123_456_789, 10**12, 10**15, 10**16,
        2**53, 2**53 + 1, 2**63 - 1, -1, -10, -100, -999_999, -1_000_000, -(2**63),
    ]
    rng = random.Random(26)  # noqa: S311 - reproducible test vectors, not secrets.
    for scale in (100, 10_000, 1_000_000, 10**9, 10**13):
        amounts += [rng.randint(0, scale) for _ in range(120)]
    return list(dict.fromkeys(amounts))


def money_vectors() -> list[dict[str, Any]]:
    return [{"microdollars": m, "repr": repr(microdollars_to_float(m))} for m in money_amounts()]


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


def string_values() -> list[str]:
    rng = random.Random(2028)  # noqa: S311 - reproducible test vectors, not secrets.
    return list(dict.fromkeys(EDGE_STRINGS + [random_string(rng) for _ in range(400)]))


def string_vectors() -> list[dict[str, Any]]:
    return [
        {
            "codepoints": [ord(c) for c in s],
            "ascii": json.dumps(s, ensure_ascii=True),
            "unicode": json.dumps(s, ensure_ascii=False),
        }
        for s in string_values()
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
        return rng.choice([value, rng.random() * _pow10(rng.randint(-8, 20)), float(rng.randint(-99, 99))])
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


AUTHORIZE_PATH = "/internal/gateway/authorize"

# The settings of a route that decide how FastAPI writes what it returns.
RESPONSE_SETTINGS = (
    "response_model", "response_class", "response_model_include", "response_model_exclude",
    "response_model_by_alias", "response_model_exclude_unset", "response_model_exclude_defaults",
    "response_model_exclude_none", "status_code",
)


def authorize_route() -> APIRoute:
    """The authorize route as gateway.register declares it."""
    router = APIRouter()
    register_gateway_internal_routes(router)
    (route,) = (r for r in router.routes if isinstance(r, APIRoute) and r.path == AUTHORIZE_PATH)
    return route


class AuthorizeBody:
    """The body FastAPI sends for a value the authorize endpoint returns: an
    endpoint declared with the real route's response settings (for authorize,
    the response model its return annotation gives, dict[str, Any], and the
    default response class) returns the value, on an app with FastAPI's
    defaults as main.create_app builds it, and the body is what a request to
    it reads back. FastAPI's own request handler writes it."""

    def __init__(self) -> None:
        real = authorize_route()
        self.settings = {name: getattr(real, name) for name in RESPONSE_SETTINGS}
        self.value: dict[str, Any] = {}

        async def endpoint() -> Any:
            return self.value

        router = APIRouter()
        router.add_api_route(AUTHORIZE_PATH, endpoint, methods=["POST"], **self.settings)
        (copy,) = router.routes
        assert {name: getattr(copy, name) for name in RESPONSE_SETTINGS} == self.settings
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, raise_server_exceptions=False)

    def __call__(self, value: dict[str, Any]) -> str | None:
        """The body, or None where FastAPI cannot write the value and answers 500."""
        self.value = value
        response = self.client.post(AUTHORIZE_PATH)
        if response.status_code == 500:
            # pydantic-core refuses a value nested deeper than its recursion
            # guard allows; nothing else here fails to serialize.
            assert _levels(value) > PYDANTIC_MAX_LEVELS, value
            return None
        assert _levels(value) <= PYDANTIC_MAX_LEVELS, value
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/json", response.headers
        return response.content.decode("utf-8")


# pydantic-core writes a value at most this many levels down, the returned
# dict the first; one deeper, scalar or container, raises "Circular reference
# detected (depth exceeded)".
PYDANTIC_MAX_LEVELS = 256


def _levels(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max(map(_levels, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(_levels, value), default=0)
    return 1


def _member(body: str, key: str) -> str:
    """The value text of a one-member object body: {"key":<value>}."""
    prefix = json.dumps({key: None}, separators=(",", ":"))[: -len("null}")]
    assert body.startswith(prefix) and body.endswith("}"), body
    return body[len(prefix) : -1]


def _required(text: str | None) -> str:
    assert text is not None
    return text


def pydantic_float_vectors(body: AuthorizeBody) -> list[dict[str, str]]:
    return [{"bits": _bits(value), "json": _member(_required(body({"v": value})), "v")} for value in float_values()]


def pydantic_money_vectors(body: AuthorizeBody) -> list[dict[str, Any]]:
    """money_pair("estimated_cost", m), as the authorize body carries it."""
    return [{"microdollars": m, "json": body(money_pair("estimated_cost", m))} for m in money_amounts()]


def pydantic_string_vectors(body: AuthorizeBody) -> list[dict[str, Any]]:
    return [{"codepoints": [ord(c) for c in s], "json": _member(_required(body({"v": s})), "v")} for s in string_values()]


def _authorize_like(rng: random.Random) -> dict[str, Any]:
    """A body shaped as _gateway_authorize_response's, its amounts drawn."""
    estimate = rng.choice([0, 1, 9, 10, 99, 100, 1234, 10_000, 99_999, rng.randint(0, 10**9)])
    return {
        "data": {
            "authorization_id": f"auth_{rng.getrandbits(64):016x}",
            "workspace_id": "ws_é😀",
            "model": "openai/gpt-4o-mini",
            "response_model": None,
            "hide_public_metadata": False,
            **money_pair("estimated_cost", estimate),
            "regions": [{"id": "us-central1", "weight": rng.random()}],
            "additional_cost_reservation_microdollars": rng.randint(0, 10**6),
            "receipt_fee_basis_points": rng.randint(0, 500),
            "tags": {random_string(rng): random_string(rng) for _ in range(rng.randint(0, 3))},
            "timing": {"total_ms": rng.randint(0, 5000), "spanner_rpcs": rng.randint(0, 9)},
            "route_candidates": [
                {"endpoint_id": f"e{i}", **money_pair("prompt_price", rng.randint(0, 10**7))}
                for i in range(rng.randint(0, 3))
            ],
        }
    }


def _nested_value(levels: int, leaf: str, kind: str) -> str:
    """A document whose leaf sits levels down inside {"data": ...}."""
    containers = levels - 2
    if kind == "list":
        return "[" * containers + leaf + "]" * containers
    return '{"k":' * containers + leaf + "}" * containers


def pydantic_document_vectors(body: AuthorizeBody) -> list[dict[str, str | None]]:
    """{"data": json.loads(text)} through the authorize route: the decoder's
    documents, values on both sides of pydantic-core's depth bound, random
    values, and bodies shaped as authorize's. "json" is None where FastAPI
    cannot write the value (a 500)."""
    rng = random.Random(4615)  # noqa: S311 - reproducible test vectors, not secrets.
    texts = list(DOCUMENTS)
    for levels in (PYDANTIC_MAX_LEVELS - 1, PYDANTIC_MAX_LEVELS, PYDANTIC_MAX_LEVELS + 1):
        for leaf in ("1", '"s"', "null", "1.5e-7", "[]", "{}"):
            for kind in ("list", "object"):
                texts.append(_nested_value(levels, leaf, kind))
    for _ in range(250):
        texts.append(json.dumps(random_value(rng, 0), ensure_ascii=rng.random() < 0.5))
    for _ in range(60):
        texts.append(json.dumps(_authorize_like(rng), ensure_ascii=rng.random() < 0.5))
    return [{"text": text, "json": body({"data": json.loads(text)})} for text in dict.fromkeys(texts)]


def render() -> str:
    """The file's text: one vector a line, so a change shows as the vectors it changes."""
    documents, refused = document_vectors()
    body = AuthorizeBody()
    sections = {
        "floats": float_vectors(),
        "money": money_vectors(),
        "strings": string_vectors(),
        "documents": documents,
        "refused": refused,
        "isoformat": isoformat_vectors(),
        "pydantic_floats": pydantic_float_vectors(body),
        "pydantic_money": pydantic_money_vectors(body),
        "pydantic_strings": pydantic_string_vectors(body),
        "pydantic_documents": pydantic_document_vectors(body),
    }
    parts = ['{"generator": "scripts/fastpath/pyjson_vectors.py"']
    for name, vectors in sections.items():
        lines = [json.dumps(v, sort_keys=True, separators=(",", ":")) for v in vectors]
        parts.append(f' "{name}": [\n  ' + ",\n  ".join(lines) + "\n ]")
    return ",\n".join(parts) + "}\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
