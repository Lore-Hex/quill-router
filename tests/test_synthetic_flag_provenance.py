"""Monitor traffic must never be indexed as real customer traffic.

Every usage and revenue query filters ``WHERE synthetic = 0``, so a probe whose
generation lands with the flag off is counted as a paying customer. That has
happened twice for structurally different reasons, and each is pinned here:

* ordinary gateway probes carry ``metadata.trustedrouter_synthetic``;
* the strict ``/videos`` API deliberately has no public metadata field, so its
  dedicated monitor key is classified server-side during authorization;
* ``from_chat_result`` and ``from_embeddings_result`` never set ``synthetic``
  at all, so the direct (non-attested) path took the ``False`` field default
  no matter what it was serving.

The first test is load-bearing: it permits exactly one server-classified probe
and fails on the next probe that forgets the marker.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from trusted_router.storage_models import SYNTHETIC_APP_NAME, Generation

PROBES_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "trusted_router"
    / "synthetic"
    / "probes.py"
)
# Six bodies exist today. The assertion is ">=" so adding a probe is allowed;
# the count guards against the test silently finding NOTHING (a rename of
# `_api_url`, a refactor to a helper) and passing vacuously.
MINIMUM_GATEWAY_PROBE_BODIES = 6
SERVER_CLASSIFIED_PROBES = {"video_generation_probe"}


def _gateway_probe_functions() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every probe function that posts a body to a gateway inference route."""
    tree = ast.parse(PROBES_PATH.read_text())
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls_api_url = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "_api_url"
            for inner in ast.walk(node)
        )
        if calls_api_url:
            found.append(node)
    return found


def _marks_synthetic(node: ast.AST) -> bool:
    """True when this function builds a dict carrying the synthetic marker."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Dict):
            continue
        for key in inner.keys:
            if isinstance(key, ast.Constant) and key.value == "trustedrouter_synthetic":
                return True
    return False


def test_gateway_probe_bodies_mark_synthetic_or_are_server_classified() -> None:
    functions = _gateway_probe_functions()

    assert len(functions) >= MINIMUM_GATEWAY_PROBE_BODIES, (
        "found only "
        f"{len(functions)} gateway probe functions; the discovery rule no longer "
        "matches the code, so this test proves nothing"
    )

    unmarked = {node.name for node in functions if not _marks_synthetic(node)}
    assert unmarked == SERVER_CLASSIFIED_PROBES, (
        "gateway probes without metadata.trustedrouter_synthetic must be "
        "explicitly classified from the dedicated monitor key; unexpected "
        f"unmarked probes: {sorted(unmarked - SERVER_CLASSIFIED_PROBES)}; "
        f"missing expected exceptions: {sorted(SERVER_CLASSIFIED_PROBES - unmarked)}"
    )


def test_discovery_rule_is_not_vacuous() -> None:
    """The marker check must be able to FAIL, not just pass."""
    stripped = ast.parse('def probe():\n    url = _api_url(base, "/chat/completions")\n')
    node = next(item for item in ast.walk(stripped) if isinstance(item, ast.FunctionDef))
    assert not _marks_synthetic(node)


class _StubResult:
    request_id = "req-stub"
    provider_name = "OpenAI"
    input_tokens = 3
    output_tokens = 4
    elapsed_seconds = 0.5
    finish_reason = "stop"
    usage_estimated = False
    first_token_seconds = None
    first_byte_seconds = None
    cached_input_tokens = 0
    reasoning_tokens = 0
    tool_calls = None


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [(SYNTHETIC_APP_NAME, True), ("TrustedRouter Gateway", False)],
    ids=["monitor", "customer"],
)
def test_direct_chat_path_records_synthetic(app_name: str, expected: bool) -> None:
    generation = Generation.from_chat_result(
        result=_StubResult(),
        workspace_id="ws-1",
        key_hash="key-1",
        model_id="openai/gpt-4.1-mini",
        app_name=app_name,
        actual_cost_microdollars=0,
        usage_type="Credits",
        streamed=False,
        provider="openai",
    )

    assert generation.synthetic is expected


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [(SYNTHETIC_APP_NAME, True), ("TrustedRouter Gateway", False)],
    ids=["monitor", "customer"],
)
def test_direct_embeddings_path_records_synthetic(app_name: str, expected: bool) -> None:
    result: dict[str, Any] = {"id": "emb-1"}
    generation = Generation.from_embeddings_result(
        result=result,
        workspace_id="ws-1",
        key_hash="key-1",
        model_id="openai/text-embedding-3-small",
        app_name=app_name,
        actual_cost_microdollars=0,
        usage_type="Credits",
        input_tokens=7,
        provider="openai",
    )

    assert generation.synthetic is expected
