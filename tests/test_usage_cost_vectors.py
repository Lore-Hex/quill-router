from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.generate_usage_cost_vectors import DESTINATION, render
from trusted_router.stage_d import endpoint_cost_microdollars_from_candidate


def test_reporting_vectors_are_generated_by_the_charge_implementation() -> None:
    assert DESTINATION.read_bytes() == render().encode()
    cases = json.loads(DESTINATION.read_text())
    assert len(cases) == 38
    for case in cases:
        usage = dict(case["usage"])
        if "cache_creation_1h_input_tokens" in usage:
            usage["cache_creation_1h_tokens"] = usage.pop("cache_creation_1h_input_tokens")
        assert endpoint_cost_microdollars_from_candidate(
            case["candidate"], **usage
        ) == case["expected_microdollars"], case["name"]


def test_original_vectors_are_unchanged() -> None:
    original = (Path(__file__).parent / "fixtures/usage_cost_vectors_main.json").read_bytes()
    assert hashlib.sha256(original).hexdigest() == "1eb3c23e28f247e1c6e9bfe09eebacca4b59fba4c468fb2950f0bbf20d79acc0"
    assert json.loads(render())[:29] == json.loads(original)
