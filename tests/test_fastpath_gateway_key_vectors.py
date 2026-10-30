"""The edge's key and workspace facts vectors are what Python's predicates compute now.

fastpath/internal/gwstate is held to fastpath/testdata/gateway_key_vectors_v1.json;
this test holds the file to the authorize route's own reading of stored key and
workspace bodies (scripts/fastpath/gateway_key_vectors.py), so a change to a
predicate, to ApiKey or Workspace, or to the tag rules fails here until the
vectors, and with them the Go facts, follow. The generator pins the expiry
clock in its own process, so it runs in a subprocess.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "fastpath" / "testdata" / "gateway_key_vectors_v1.json"
GATEWAY = ROOT / "src" / "trusted_router" / "routes" / "internal" / "gateway.py"


def _vectors() -> dict[str, Any]:
    return json.loads(VECTORS.read_text())


def test_the_vectors_are_what_the_python_predicates_compute_now() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(ROOT / "scripts" / "fastpath" / "gateway_key_vectors.py"), "--print"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert VECTORS.read_text() == result.stdout, (
        "fastpath/testdata/gateway_key_vectors_v1.json is stale: run "
        "`uv run python scripts/fastpath/gateway_key_vectors.py` and check fastpath/internal/gwstate against it"
    )


def test_every_reason_is_exercised_on_well_formed_bodies() -> None:
    vectors = _vectors()
    exact = Counter(case["python"]["reason"] for case in vectors["key_cases"] if case["exact"])
    for reason in [None, *vectors["fact_order"]]:
        assert exact[reason] >= 5, (reason, exact)
    assert "error" not in exact
    workspaces = Counter(case["python"]["reason"] for case in vectors["workspace_cases"] if case["exact"])
    for reason in [None, *vectors["workspace_order"]]:
        assert workspaces[reason] >= 1, (reason, workspaces)
    assert "error" not in workspaces


def test_the_expiry_clock_is_the_case_instant() -> None:
    # Positive control for the pinned clock: one microsecond either side of
    # the instant flips the verdict, so the wall clock is not what decided.
    cases = {case["name"]: case["python"]["reason"] for case in _vectors()["key_cases"]}
    for form in ("z", "offset_zero", "naive", "offset_east", "offset_west", "space"):
        assert cases[f"expires_past_us_{form}"] == "key_expired"
        assert cases[f"expires_at_{form}"] == "key_expired"
        assert cases[f"expires_future_us_{form}"] is None
    # fromisoformat truncates past the microsecond: .250000999 is the instant.
    assert cases["expires_fraction_250000999"] == "key_expired"


def test_the_bodies_are_synthetic() -> None:
    for case in _vectors()["key_cases"]:
        assert "sk-tr" not in case["body"], case["name"]


def _route_reads() -> set[str]:
    """Every read of the key or the workspace in the authorize route, from the
    key read to the end of its _fastpath_admission call: ``api_key.x``,
    ``getattr(api_key, "x", ...)`` and each call given ``api_key`` or
    ``workspace`` (as ``workspace.x`` and the workspace's calls)."""
    tree = ast.parse(GATEWAY.read_text())
    route = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_authorize_gateway_sync_impl")
    admissions = [node for node in ast.walk(route) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "_fastpath_admission"]
    assert len(admissions) == 1
    end = admissions[0].end_lineno or 0
    names = {"api_key", "workspace"}
    reads: set[str] = set()
    for node in ast.walk(route):
        if getattr(node, "lineno", end + 1) > end:
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in names:
            reads.add(f"{node.value.id}.{node.attr}")
        if not isinstance(node, ast.Call):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in names:
                reads.add(f"{target.id}.{ast.literal_eval(node.args[1])}")
            continue
        if any(isinstance(argument, ast.Name) and argument.id in names for argument in arguments):
            reads.add(ast.unparse(node.func))
    return reads


def test_the_oracle_covers_every_key_and_workspace_read_on_the_authorize_path() -> None:
    reads = _route_reads()
    # Positive control: the walk finds the route's reads, the shard count's
    # among them.
    assert {"api_key.disabled", "key_usage_shard_count", "_oauth_app_terms_for_key", "workspace.id"} <= reads
    covered = set(_vectors()["route_reads"])
    assert reads - covered == set(), (
        "the authorize route reads the key or workspace in ways the oracle does not cover: "
        "add each to ROUTE_READS and its check to key_facts/workspace_facts in "
        "scripts/fastpath/gateway_key_vectors.py, then regenerate"
    )
    assert covered - reads == set(), "ROUTE_READS names reads the route no longer makes"


def test_alert_only_windows_are_their_own_reason() -> None:
    # enforced_window_limits is {} for an alert-only key, so its windows are
    # key_window_alert; false or absent enforces them (key_window_limit), and
    # without a window the flag decides nothing.
    cases = {case["name"]: case for case in _vectors()["key_cases"]}
    reason = {name: case["python"]["reason"] for name, case in cases.items()}
    for window in ("daily", "weekly", "monthly"):
        for value in (0, 500):
            assert reason[f"window_{window}_{value}_alert_only"] == "key_window_alert"
            assert reason[f"window_{window}_{value}"] == "key_window_limit"
    assert reason["window_alert_only_false"] == reason["window_alert_only_absent"] == "key_window_limit"
    assert reason["alert_only_no_window"] is None and reason["window_null_alert_only"] is None
    # A lifetime cap still comes first, and strict after.
    assert reason["window_alert_only_cap"] == "key_limit"
    assert reason["window_alert_only_strict"] == "key_window_alert"
    # Python reads the flag by truthiness; these bodies are not exact, Go
    # passes them.
    assert reason['window_alert_only_type_"false"'] == "key_window_alert"
    assert reason["window_alert_only_type_0"] == "key_window_limit"
    assert not any(cases[f"window_alert_only_type_{v}"]["exact"] for v in ("1", "0", '"false"', "null"))


def test_the_shard_count_vectors_hold_python_to_its_raise() -> None:
    cases = {case["name"]: case for case in _vectors()["key_cases"]}
    for value in ("0", "-1", "65", "true", "false", "null", "[]", "{}", '"abc"'):
        assert cases[f"usage_shard_count_bad_{value}"]["python"]["reason"] == "error", value
    for count in (1, 8, 64):
        assert cases[f"usage_shard_count_{count}"]["python"]["reason"] is None
