from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rerank_vectors_match_python():
    result = subprocess.run(  # noqa: S603 - repository generator
        [sys.executable, str(ROOT / "scripts/fastpath/rerank_vectors.py"), "--print"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr
    committed = (ROOT / "fastpath/testdata/authorize_rerank_v1.json").read_text()
    assert committed == result.stdout
    cases = json.loads(committed)["cases"]
    assert {case["name"]: case["expect"]["python"] for case in cases} == {
        "pass:rerank:accepted": {"leased": True, "status": 200, "type": None},
        "pass:rerank:chat": {"leased": False, "status": 400, "type": "model_not_supported"},
        "pass:rerank:absent": {"leased": False, "status": 400, "type": "model_not_supported"},
    }
    for case in cases:
        assert case["expect"]["outcome"] == "pass"
        assert case["expect"]["reason"] == "body.evaluator_input"
