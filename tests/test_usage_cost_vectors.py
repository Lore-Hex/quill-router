from __future__ import annotations

import json

from scripts.generate_usage_cost_vectors import DESTINATION, render
from trusted_router.stage_d import endpoint_cost_microdollars_from_candidate


def test_reporting_vectors_are_generated_by_the_charge_implementation() -> None:
    assert DESTINATION.read_bytes() == render().encode()
    cases = json.loads(DESTINATION.read_text())
    assert len(cases) == 29
    for case in cases:
        assert endpoint_cost_microdollars_from_candidate(
            case["candidate"], **case["usage"]
        ) == case["expected_microdollars"], case["name"]
