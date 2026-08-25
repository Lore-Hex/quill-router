"""The published OpenAPI spec has to survive being turned into LLM tools.

A client that generates function-calling tools from this spec names each tool
after its operationId. OpenAI-style function calling caps that name at 64
characters and allows only [a-zA-Z0-9_-], and the OpenAPI spec itself requires
operationId to be unique across the whole document.

Violating either is invisible in normal use: the spec still validates, the
endpoint still works over HTTP, and only the generated client breaks -- on the
one endpoint it silently cannot name.

These assert against the SHIPPED asset rather than app.openapi(), because the
public surface serves the pre-serialized file and the generator rewrites every
operationId. Testing the live app would measure a document nobody is served.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

SPEC_PATH = Path(__file__).resolve().parents[1] / "src/trusted_router/static/openapi-public.json"
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _operations() -> list[tuple[str, str, dict]]:
    spec = json.loads(SPEC_PATH.read_text())
    return [
        (path, method, operation)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS and isinstance(operation, dict)
    ]


def test_every_operation_id_is_a_legal_tool_name() -> None:
    """The failure this catches: get_compare_models_left_author_left_slug_vs_
    right_author_right_slug, 67 characters, generated from a long path."""
    offenders = [
        (f"{method.upper()} {path}", operation["operationId"])
        for path, method, operation in _operations()
        if "operationId" in operation and not TOOL_NAME.match(operation["operationId"])
    ]

    assert not offenders, "\n".join(
        f"{where}: {len(name)} chars, {name}" for where, name in offenders
    )


def test_operation_ids_are_unique() -> None:
    """Required by OpenAPI, and load-bearing for tool generation: two tools
    with one name means one of them is unreachable."""
    ids = [op["operationId"] for _, _, op in _operations() if "operationId" in op]
    repeated = {name: count for name, count in Counter(ids).items() if count > 1}

    assert not repeated, repeated


def test_every_operation_has_an_id() -> None:
    """An operation with no id cannot become a tool at all."""
    missing = [
        f"{method.upper()} {path}"
        for path, method, operation in _operations()
        if not operation.get("operationId")
    ]

    assert not missing, missing


def test_every_operation_describes_itself() -> None:
    """The description is what the model reads to decide whether to call it.
    An unnamed, undescribed tool is one the model will either never pick or
    pick for the wrong reason."""
    silent = [
        f"{method.upper()} {path}"
        for path, method, operation in _operations()
        if not (operation.get("summary") or operation.get("description"))
    ]

    assert not silent, silent


@pytest.mark.parametrize(
    "path",
    ["/v1/chat/completions", "/v1/models", "/v1/providers"],
)
def test_the_core_endpoints_have_typed_responses(path: str) -> None:
    """Scoped deliberately to the endpoints an agent actually calls.

    106 of 165 /v1 operations still return an untyped 200, mostly auth and
    console routes no agent invokes. Typing all of them is a refactor, not a
    fix, and asserting it here would be a failing test standing in for a
    decision nobody has made. These three are the inference surface and are
    typed today; this keeps them that way.
    """
    spec = json.loads(SPEC_PATH.read_text())
    item = spec["paths"][path]
    method = "post" if "post" in item else "get"
    schema = item[method]["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema, f"{method.upper()} {path} returns an untyped 200"
