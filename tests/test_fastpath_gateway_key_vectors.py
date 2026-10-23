"""The edge's key and workspace facts vectors are what Python's predicates compute now.

fastpath/internal/gwstate is held to fastpath/testdata/gateway_key_vectors_v1.json;
this test holds the file to the authorize route's own reading of stored key and
workspace bodies (scripts/fastpath/gateway_key_vectors.py), so a change to a
predicate, to ApiKey or Workspace, or to the tag rules fails here until the
vectors, and with them the Go facts, follow. The generator pins the expiry
clock in its own process, so it runs in a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "fastpath" / "testdata" / "gateway_key_vectors_v1.json"


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
