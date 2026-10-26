"""The routing golden cases are what the Python candidate pipeline computes now.

fastpath/testdata/routing_cases_v1.json maps requests over the routing
snapshot's fixture catalog to their candidates, model estimate and Stage D
reason. The generator installs that catalog in its own process, so this test
runs it in a subprocess, and then checks that the snapshot body alone prices
and admits every answer, which is what the Go evaluator will rely on.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from trusted_router.stage_d import endpoint_cost_microdollars_from_candidate

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "routing_cases_v1.json"
SNAPSHOT = ROOT / "fastpath" / "testdata" / "routing_snapshot_v3.body.json"


def _cases() -> dict[str, Any]:
    return json.loads(CASES.read_text())


def test_the_cases_are_what_the_python_pipeline_computes_now() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(ROOT / "scripts" / "fastpath" / "routing_cases_vectors.py"), "--print"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert CASES.read_text() == result.stdout, (
        "fastpath/testdata/routing_cases_v1.json is stale: run "
        "`uv run python scripts/fastpath/routing_cases_vectors.py` and check the Go evaluator"
    )


def test_the_cases_name_the_snapshot_they_were_computed_over() -> None:
    assert _cases()["snapshot_sha256"] == hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()


def _seconds(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def test_the_snapshot_alone_admits_and_prices_every_answer() -> None:
    snapshot = json.loads(SNAPSHOT.read_bytes())
    endpoints = {endpoint["id"]: endpoint for endpoint in snapshot["endpoints"]}
    plane_region = _cases()["settings"]["primary_region"]
    answered = refused = 0
    for case in _cases()["cases"]:
        expect, request = case["expect"], case["request"]
        at = _seconds(case["at"])
        assert snapshot["compiled_at"] <= at < snapshot["not_after"], case["name"]
        if "error" in expect:
            refused += 1
            assert expect["error"]["status"] >= 400 and expect["error"]["message"]
            continue
        answered += 1
        assert expect["candidates"], case["name"]
        prices = []
        for endpoint_id in expect["candidates"]:
            endpoint = endpoints[endpoint_id]
            assert endpoint["catalog_valid_until"] is None or at < endpoint["catalog_valid_until"]
            assert endpoint["retired_at"] is None or at < endpoint["retired_at"]
            rule = endpoint["gateway_regions"]
            # The authorize schema strips the region (str_strip_whitespace).
            region = (request.get("region") or "").strip() or (
                "" if rule["requires_explicit_origin"] else plane_region
            )
            assert (rule["only"] is None or region in rule["only"]) and region not in rule["deny"]
            if endpoint["usage_type"] == "BYOK":
                assert endpoint["provider"] in case["byok_providers"], case["name"]
            epoch = [e for e in endpoint["epochs"] if e["starts_at"] <= at][-1]
            prices.append(endpoint_cost_microdollars_from_candidate(
                epoch["price"], request["estimated_input_tokens"], request["output_tokens"],
            ))
        if expect["service_tier"] is None:
            # The estimate is the largest candidate's price at the request's instant.
            assert expect["model_estimate"] == max(prices), case["name"]
    # Positive control: the cases both admit and refuse in volume, and some
    # order several candidates.
    assert answered > 60 and refused > 30
    assert sum(len(c["expect"].get("candidates", [])) > 2 for c in _cases()["cases"]) > 10
