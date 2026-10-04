"""Generate reporting conformance vectors from the frozen Python charge formula.

Run: .venv/bin/python -m scripts.generate_usage_cost_vectors
Copy the output unchanged to enclave-go/internal/trustedrouter/testdata/ in
quill-cloud-proxy. Neither the generator nor its tests contact a service.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trusted_router.stage_d import (
    PRICE_HISTORY_VERSION,
    PRICING_ROUNDING,
    endpoint_cost_microdollars_from_candidate,
)

DESTINATION = Path(__file__).resolve().parents[1] / "tests/fixtures/usage_cost_vectors.json"


def vectors() -> list[dict[str, Any]]:
    def rates(i: int, o: int, r: int, w: int) -> dict[str, int]:
        return dict(zip(("input_micro_per_million", "output_micro_per_million",
                         "cached_input_micro_per_million", "cache_creation_micro_per_million"),
                        (i, o, r, w), strict=True))

    base = rates(1_000_000, 3_000_000, 100_000, 1_250_000)
    cases: list[dict[str, Any]] = []

    def add(name: str, *, selected_rates: dict[str, int] | None = None,
            fee: int = 0, tiers: list[dict[str, Any]] | None = None,
            i: int = 0, o: int = 0, r: int = 0, w: int = 0, tier: int = 0) -> None:
        candidate = {
            "endpoint_id": "fixture-endpoint",
            "price_history_version": PRICE_HISTORY_VERSION,
            "rounding": PRICING_ROUNDING,
            "rates": selected_rates if selected_rates is not None else base,
            "tiers": tiers or [], "request_fee_micro": fee,
        }
        usage = {"input_tokens": i, "output_tokens": o, "cache_read_tokens": r,
                 "cache_creation_tokens": w, "price_tier_input_tokens": tier}
        cases.append({"name": name, "candidate": candidate, "usage": usage,
                      "expected_microdollars": endpoint_cost_microdollars_from_candidate(
                          candidate, **usage)})

    add("zero-usage")
    add("free-tokens", selected_rates=rates(0, 0, 0, 0), i=100, o=20, r=30, w=40)
    add("request-fee-only", fee=17)
    add("all-components", fee=7, i=11, o=13, r=17, w=19)
    add("large-exact-integer", selected_rates=rates(9_007_199_254_740_993, 0, 0, 0), i=1_000_000)
    for component in ("i", "o", "r", "w"):
        add(f"minimum-{component}", selected_rates=rates(1, 1, 1, 1), **{component: 1})
        for count in (499_999, 500_000, 500_001):
            add(f"half-up-{component}-{count}", selected_rates=rates(1, 1, 1, 1),
                **{component: count})
    add("round-components-before-sum", selected_rates=rates(1, 1, 1, 1),
        i=500_000, o=500_000, r=500_000, w=500_000)
    tiers = [{"max_prompt_tokens": 100, "rates": base},
             {"max_prompt_tokens": None, "rates": rates(2_000_000, 6_000_000, 200_000, 2_500_000)}]
    for count in (99, 100, 101):
        add(f"tier-{count}", tiers=tiers, i=count, o=7)
    add("cache-counts-select-tier", tiers=tiers, i=90, r=8, w=3, o=7)
    add("private-tier-basis", tiers=tiers, i=90, r=8, w=3, o=7, tier=100)
    add("invalid-private-tier-basis", tiers=tiers, i=90, r=8, w=3, o=7, tier=102)
    add("last-bounded-tier", tiers=[tiers[0]], i=101, o=7,
        selected_rates=rates(9_000_000, 9_000_000, 0, 0))
    return cases


def render() -> str:
    return json.dumps(vectors(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    DESTINATION.write_text(render())
