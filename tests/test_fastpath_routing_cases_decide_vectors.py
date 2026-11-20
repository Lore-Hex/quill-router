"""The separate hosted decide fixture and Python answers must remain current."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from trusted_router.routing_snapshot import Plane, parse_routing_snapshot
from trusted_router.routing_snapshot_signing import verify_manifest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "fastpath/testdata"


def test_decide_cases_are_the_real_python_pipeline() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository generator
        [sys.executable, str(ROOT / "scripts/fastpath/routing_cases_decide_vectors.py"), "--print"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (DATA / "routing_cases_decide_v1.json").read_text() == result.stdout, (
        "Run scripts/fastpath/routing_cases_decide_vectors.py; hosted decide cases are stale"
    )


def test_decide_snapshot_and_manifest_are_current() -> None:
    path = ROOT / "scripts/fastpath/routing_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("decide_snapshot_vectors", path)
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    body = (DATA / "routing_snapshot_decide_v3.body.json").read_bytes()
    manifest_text = (DATA / "routing_snapshot_decide_v3.manifest.json").read_text()
    assert body == generator.render_body(hosted_decide=True), "Run routing_snapshot_vectors.py --decide"
    assert manifest_text == generator.render_manifest(hosted_decide=True)
    manifest = json.loads(manifest_text)
    snapshot = parse_routing_snapshot(body)
    claims = verify_manifest(
        manifest["jws"], [generator.TEST_KEY], body, now=snapshot.compiled_at,
        expected_plane=Plane(cloud="gcp", region="us-central1"),
        expected_aud=generator.TEST_KEY.aud, allow_test_key=True,
    )
    cases = json.loads((DATA / "routing_cases_decide_v1.json").read_text())
    assert cases["snapshot_sha256"] == claims.sha256 == hashlib.sha256(body).hexdigest()
    assert cases["snapshot"] == "fastpath/testdata/routing_snapshot_decide_v3.body.json"
    # Original entry points must still render the original committed bytes.
    assert generator.render_body() == (DATA / "routing_snapshot_v3.body.json").read_bytes()
    assert generator.render_manifest() == (DATA / "routing_snapshot_v3.manifest.json").read_text()


def test_decide_vectors_make_the_arm_differences_observable() -> None:
    cases = {case["name"]: case for case in json.loads((DATA / "routing_cases_decide_v1.json").read_text())["cases"]}
    snapshot = json.loads((DATA / "routing_snapshot_decide_v3.body.json").read_text())
    models = {model["id"]: model for model in snapshot["models"]}
    over = cases["over_context"]
    assert over["request"]["estimated_input_tokens"] > models[over["request"]["model"]]["context_length"]
    assert len(over["expect"]["candidates"]) == 2
    for name in ("parameters_fixed_error", "privacy_all_removed", "deny_all_removed", "ignore_all"):
        assert cases[name]["expect"]["error"] == {
            "status": 400, "type": "model_not_supported",
            "message": "No route candidates match the requested provider filters",
        }
    assert len(cases["deny"]["expect"]["candidates"]) == 1
    assert len(cases["deny_soft"]["expect"]["candidates"]) == 1
    assert cases["order"]["expect"]["candidates"] != cases["multi"]["expect"]["candidates"]
    assert cases["multi"]["expect"]["model_estimate"] == 2468
    assert cases["only"]["expect"]["model_estimate"] == 617
    assert cases["output_hold"]["expect"]["model_reservation"] == 2468
    assert cases["stream"]["expect"]["stage_d_reason"] == "route"
