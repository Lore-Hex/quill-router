"""The Go port of canonical_router_model_id is held to what Python answers now.

fastpath/internal/gwauth checks every entry of
fastpath/testdata/model_alias_vectors_v1.json; this test holds the file to
scripts/fastpath/model_alias_vectors.py's output over the live catalog and
model_aliases.py, so a new alias, rule or catalog model fails here until the
vectors, and with them the Go port, follow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from trusted_router.catalog import MODELS
from trusted_router.model_aliases import _ROUTER_MODEL_ID_ALIASES

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "fastpath" / "model_alias_vectors.py"
VECTORS = ROOT / "fastpath" / "testdata" / "model_alias_vectors_v1.json"


def test_the_vectors_are_what_python_answers_now() -> None:
    # In a subprocess: the generator clears TR_* from its own environment.
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(GENERATOR), "--print"],
        env=os.environ.copy(), cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert VECTORS.read_text() == result.stdout, (
        "fastpath/testdata/model_alias_vectors_v1.json is stale: run "
        "uv run python scripts/fastpath/model_alias_vectors.py and update the Go port if a case fails"
    )


def test_the_vectors_cover_every_rule() -> None:
    vectors = json.loads(VECTORS.read_text())
    cases = {case["id"]: case["canonical"] for case in vectors["cases"]}
    assert vectors["aliases"] == dict(sorted(_ROUTER_MODEL_ID_ALIASES.items()))
    assert set(MODELS) <= set(cases)
    for alias, canonical in _ROUTER_MODEL_ID_ALIASES.items():
        assert cases[alias] == canonical
    # Each rewrite rule, and an identity, is exercised (positive control).
    assert cases["system1models/x"] == "decisionmodelsio/x"
    assert cases["system1models-eu/x"] == "decisionmodelsio-eu/x"
    assert cases["nyte/auto"] == "trustedrouter/auto"
    assert cases["trustedrouter/auto-routing:floor"] == "trustedrouter/auto:floor"
    assert cases["z-ai/glm-5.3"] == "z-ai/glm-5.3"
    rewritten = sum(1 for model_id, canonical in cases.items() if model_id != canonical)
    assert 100 < rewritten < len(cases)
