"""The Go JSON writer's golden vectors are what CPython writes now.

fastpath/internal/pyjson is held to fastpath/testdata/pyjson_vectors_v1.json;
this test holds the file to CPython's float repr, json.dumps, json.loads,
storage_codec.json_body, Starlette's JSONResponse and
storage_gcp._iso_timestamp, so a change to any of them (or to the
interpreter) fails here until the vectors, and with them the port, follow.

The pydantic_* vectors are the authorize route's 200 body, which FastAPI
writes with pydantic-core rather than json.dumps; the generator reads them
from an endpoint declared with the route's response settings, and the real
app's authorize route is held here to writing every one of them.
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import money_pair
from trusted_router.routes.internal import gateway

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "fastpath" / "pyjson_vectors.py"
VECTORS = ROOT / "fastpath" / "testdata" / "pyjson_vectors_v1.json"


def test_the_vectors_are_what_cpython_writes_now() -> None:
    # In a subprocess: the generator clears TR_* from its environment, which
    # must not leak into this process's later tests.
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(GENERATOR), "--print"],
        env=os.environ.copy(), cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert VECTORS.read_text() == result.stdout, (
        "fastpath/testdata/pyjson_vectors_v1.json is stale: run "
        "`uv run python scripts/fastpath/pyjson_vectors.py` and check the Go port against it"
    )


def test_the_vectors_hold_the_cases_the_port_must_get_right() -> None:
    vectors = json.loads(VECTORS.read_text())
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
    pydantic = {v["json"] for v in vectors["pydantic_floats"]}
    for named in ("0.00001", "0.000015", "9.999999999999999e-6", "1e-6", "1e-10", "0.0001", "1e+16",
                  "9999999999999998.0", "5e-324", "1.7976931348623157e+308", "-0.0", "1.0", "null"):
        assert named in pydantic, named
    assert {v["bits"] for v in vectors["pydantic_floats"]} == {v["bits"] for v in vectors["floats"]}
    assert {v["microdollars"] for v in vectors["pydantic_money"]} == {v["microdollars"] for v in vectors["money"]}
    assert '{"estimated_cost":1e-6,"estimated_cost_microdollars":1}' in {v["json"] for v in vectors["pydantic_money"]}
    unwritable = [v["text"] for v in vectors["pydantic_documents"] if v["json"] is None]
    assert len(unwritable) >= 10, "the documents must cross pydantic-core's depth bound"


class _RealAuthorizeRoute:
    """The app's own authorize route, middleware and all, made to return a
    given value: what it sends is what FastAPI writes for authorize's 200."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.value: dict[str, Any] = {}

        async def returns_value(*_args: object) -> dict[str, Any]:
            return self.value

        monkeypatch.setattr(gateway, "authorize_gateway", returns_value)
        app = create_app(Settings(environment="test"), init_observability=False)
        self.client = TestClient(app, raise_server_exceptions=False)

    def __call__(self, value: dict[str, Any]) -> tuple[int, str]:
        self.value = value
        response = self.client.post("/v1/internal/gateway/authorize", json={"api_key_hash": "k", "model": "m"})
        if response.status_code == 200:
            assert response.headers["content-type"] == "application/json"
        return response.status_code, response.content.decode("utf-8")


def _float(bits: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


def test_the_real_authorize_route_writes_the_pydantic_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generator reads the pydantic_* vectors from an endpoint declared
    with the authorize route's response settings; the real app's route writes
    every one of them byte for byte, and answers 500 for each document the
    vectors mark unwritable."""
    vectors = json.loads(VECTORS.read_text())
    route = _RealAuthorizeRoute(monkeypatch)

    def listed(items: list[Any], texts: list[str]) -> None:
        assert route({"data": items}) == (200, '{"data":[' + ",".join(texts) + "]}")

    floats = vectors["pydantic_floats"]
    assert any(math.isnan(_float(v["bits"])) for v in floats)
    listed([_float(v["bits"]) for v in floats], [v["json"] for v in floats])
    strings = vectors["pydantic_strings"]
    listed(["".join(map(chr, v["codepoints"])) for v in strings], [v["json"] for v in strings])
    money = vectors["pydantic_money"]
    listed([money_pair("estimated_cost", v["microdollars"]) for v in money], [v["json"] for v in money])
    for document in vectors["pydantic_documents"]:
        status, body = route({"data": json.loads(document["text"])})
        if document["json"] is None:
            assert status == 500, document["text"][:60]
        else:
            assert (status, body) == (200, document["json"]), document["text"][:60]
