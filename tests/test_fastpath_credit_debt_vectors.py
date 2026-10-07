"""The Go port's golden vectors are what the Python debt rules compute now.

fastpath/internal/creditdebt is held to fastpath/testdata/credit_debt_vectors.json;
this test holds the file to `trusted_router.credit_debt`, so a change to the
Python rules fails here until the vectors, and with them the port, follow.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    path = ROOT / "scripts" / "fastpath" / "credit_debt_vectors.py"
    spec = importlib.util.spec_from_file_location("credit_debt_vectors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_vectors_are_what_the_python_rules_compute_now() -> None:
    generator = _generator()
    assert generator.OUTPUT.read_text() == generator.render(), (
        "fastpath/testdata/credit_debt_vectors.json is stale: run "
        "`uv run python scripts/fastpath/credit_debt_vectors.py` and check the Go port against it"
    )
