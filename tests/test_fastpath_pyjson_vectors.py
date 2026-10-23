"""The Go JSON writer's golden vectors are what CPython writes now.

fastpath/internal/pyjson is held to fastpath/testdata/pyjson_vectors_v1.json;
this test holds the file to CPython's float repr, json.dumps, json.loads,
storage_codec.json_body, Starlette's JSONResponse and
storage_gcp._iso_timestamp, so a change to any of them (or to the
interpreter) fails here until the vectors, and with them the port, follow.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    path = ROOT / "scripts" / "fastpath" / "pyjson_vectors.py"
    spec = importlib.util.spec_from_file_location("pyjson_vectors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_vectors_are_what_cpython_writes_now() -> None:
    generator = _generator()
    assert generator.OUTPUT.read_text() == generator.render(), (
        "fastpath/testdata/pyjson_vectors_v1.json is stale: run "
        "`uv run python scripts/fastpath/pyjson_vectors.py` and check the Go port against it"
    )


def test_the_vectors_hold_the_cases_the_port_must_get_right() -> None:
    vectors = json.loads(_generator().OUTPUT.read_text())
    reprs = {v["repr"] for v in vectors["floats"]}
    for named in ("1e-05", "0.0001", "1e+16", "9999999999999998.0", "5e-324", "1.7976931348623157e+308",
                  "0.30000000000000004", "-0.0", "1.0", "1000000000000000.0"):
        assert named in reprs, named
    codepoints = {c for v in vectors["strings"] for c in v["codepoints"]}
    assert set(range(0x20)) | {0x7F, 0x2028, 0x2029, 0x1F600, 0x10FFFF} <= codepoints
    reasons = {v.get("go_refuses") for v in vectors["refused"]}
    assert reasons == {None, "duplicate_key", "lone_surrogate", "non_finite", "too_deep"}
    assert any(v["isoformat"].endswith(":00Z") and "." not in v["isoformat"] for v in vectors["isoformat"])
    assert any(len(v["isoformat"]) == len("2026-10-23T17:04:05.000010Z") for v in vectors["isoformat"])
