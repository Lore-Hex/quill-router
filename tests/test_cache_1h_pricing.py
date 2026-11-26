"""Frozen-main comparisons and integer rounding properties for cache lifetimes."""
from __future__ import annotations

import copy
import hashlib
import json
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.test_billing_snapshot_catalog import ORDINARY, PINNED, _grid
from trusted_router import billing_snapshot as billing
from trusted_router.authorize_candidates import endpoint_cost_microdollars
from trusted_router.money import token_cost_microdollars
from trusted_router.openai_service_tiers import OpenAIPriorityPricing
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_candidate as cost,
)
from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_document,
    endpoint_pricing_candidate,
    endpoint_pricing_document,
)

# origin/main and checkout HEAD at the start: fea889b89c264397c1b7f8198df3ae4c45116250.
FROZEN = Path(__file__).parent / "fakes/cache_1h_main_stage_d.py.txt"


def _main() -> types.ModuleType:
    source = FROZEN.read_bytes()
    assert hashlib.sha256(source).hexdigest() == "59026e2577d27f9e2de3ae11fdc447692ae2eac9cf854ec29d010e868c8592f6"
    module = types.ModuleType("cache_1h_frozen_main")
    exec(compile(source, str(FROZEN), "exec"), module.__dict__)  # noqa: S102 - pinned main source
    return module


@pytest.mark.parametrize("endpoint", PINNED, ids=lambda e: e.id)
def test_absent_none_and_zero_match_main_bytes(endpoint: Any) -> None:
    main = _main()
    snapshot = billing.build_snapshot([endpoint], ORDINARY)
    candidate = endpoint_pricing_candidate(endpoint)
    document = endpoint_pricing_document([endpoint])
    assert document == main.endpoint_pricing_document([endpoint])
    for reported, output, read, written in _grid(snapshot.candidates[0]):
        for field in ({}, {"cache_creation_1h_input_tokens": None}, {"cache_creation_1h_input_tokens": 0}):
            body = GatewaySettleRequest(authorization_id="a", cache_creation_input_tokens=written, **field)
            total = reported if snapshot.candidates[0].prompt_convention == "includes_cache" else reported + read + written
            uncached = total - read - written
            for basis in (None, 0, total, max(1, total - 1), total + 1):
                usage = dict(cache_read_tokens=read, cache_creation_tokens=written, price_tier_input_tokens=basis)
                expected = main.endpoint_cost_microdollars_from_candidate(candidate, uncached, output, **usage)
                actual = cost(candidate, uncached, output, **usage, cache_creation_1h_tokens=body.cache_creation_1h_count)
                assert json.dumps(actual).encode() == json.dumps(expected).encode()
                assert cost(candidate, uncached, output, **usage) == expected
                assert endpoint_cost_microdollars_from_document(document, endpoint.id, uncached, output,
                    **usage, cache_creation_1h_tokens=body.cache_creation_1h_count) == expected
                assert endpoint_cost_microdollars(endpoint, uncached, output,
                    **usage, cache_creation_1h_tokens=body.cache_creation_1h_count) == expected


@pytest.mark.parametrize("value", [-1, 1.5, "no", [], {}])
def test_invalid_count_matches_creation_validation(value: Any) -> None:
    for field in ("cache_creation_input_tokens", "cache_creation_1h_input_tokens"):
        with pytest.raises(ValidationError):
            GatewaySettleRequest(authorization_id="a", **{field: value})


def _candidate(rate: int, creation: int) -> dict[str, Any]:
    return {"rounding": "half_up_per_million", "request_fee_micro": 7, "tiers": [], "rates": {
        "input_micro_per_million": rate, "output_micro_per_million": 0,
        "cached_input_micro_per_million": 0, "cache_creation_micro_per_million": creation,
    }}


@settings(max_examples=250, database=None)
@given(rate=st.integers(0, 10**12), total=st.integers(0, 10**9),
       a=st.integers(0, 10**9), b=st.integers(0, 10**9))
def test_split_rounding_bounds_and_all_hour_identity(rate: int, total: int, a: int, b: int) -> None:
    creation = int((Decimal(rate) * Decimal("1.25")).to_integral_value())
    candidate = _candidate(rate, creation)
    low, high = sorted((min(a, total), min(b, total)))
    def charge(count: int) -> int:
        return cost(candidate, 0, 0, cache_creation_tokens=total, cache_creation_1h_tokens=count)
    difference = charge(high) - charge(low)
    expected = token_cost_microdollars(high - low, 2 * rate - creation)
    # Four independently rounded terms have total error at most two microdollars.
    assert abs(difference - expected) <= 2
    assert difference >= -1
    all_hour = copy.deepcopy(candidate)
    all_hour["rates"]["cache_creation_micro_per_million"] = 2 * rate
    assert charge(total) == cost(all_hour, 0, 0, cache_creation_tokens=total)
    assert charge(total + 1) == charge(total)


@settings(max_examples=150, database=None)
@given(rate=st.integers(0, 10**6), total=st.integers(0, 10**8), a=st.integers(0, 10**8), b=st.integers(0, 10**8))
def test_integral_per_token_prices_are_monotone(rate: int, total: int, a: int, b: int) -> None:
    candidate = _candidate(rate * 4_000_000, rate * 5_000_000)
    low, high = sorted((min(a, total), min(b, total)))
    assert cost(candidate, 0, 0, cache_creation_tokens=total, cache_creation_1h_tokens=low) <= cost(
        candidate, 0, 0, cache_creation_tokens=total, cache_creation_1h_tokens=high)


def test_separate_rounding_can_decrease_by_one_microdollar() -> None:
    candidate = _candidate(1, 1)
    assert cost(candidate, 0, 0, cache_creation_tokens=500_000) == 8
    assert cost(candidate, 0, 0, cache_creation_tokens=500_000, cache_creation_1h_tokens=1) == 7


def test_hour_minimum_with_zero_creation_rate() -> None:
    candidate = _candidate(1, 0)
    candidate["request_fee_micro"] = 0
    assert cost(candidate, 0, 0, cache_creation_tokens=1) == 0
    assert cost(candidate, 0, 0, cache_creation_tokens=1, cache_creation_1h_tokens=1) == 1


def test_priority_uses_its_customer_input_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import openai_service_tiers as priority
    from trusted_router.catalog_data import ModelEndpoint

    monkeypatch.setattr(priority, "openai_priority_pricing", lambda *args, **kwargs: OpenAIPriorityPricing(
        2_000_000, 200_000, 6_000_000, 2_500_000))
    endpoint = ModelEndpoint(id="fixture", model_id="openai/fixture", provider="openai", usage_type="Credits")
    assert endpoint_cost_microdollars(endpoint, 14, 6, cache_read_tokens=100,
        cache_creation_tokens=2000, cache_creation_1h_tokens=800, service_tier="priority") == 28 + 36 + 20 + 3000 + 3200
    assert endpoint_cost_microdollars(endpoint, 14, 6, cache_read_tokens=100,
        cache_creation_tokens=2000, service_tier="priority") == 28 + 36 + 20 + 5000
    # The priority formula bounds the count itself: above the total it is the
    # total (all 2000 writes at twice the $2/M input rate), below zero it is zero.
    assert endpoint_cost_microdollars(endpoint, 14, 6, cache_read_tokens=100,
        cache_creation_tokens=2000, cache_creation_1h_tokens=3000, service_tier="priority") == 28 + 36 + 20 + 8000
    assert endpoint_cost_microdollars(endpoint, 14, 6, cache_read_tokens=100,
        cache_creation_tokens=2000, cache_creation_1h_tokens=-5, service_tier="priority") == 28 + 36 + 20 + 5000


def test_async_usage_contract_stays_closed_to_hour_count() -> None:
    for field in ("cache_creation_1h_input_tokens", "cache_creation_1h_tokens"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            billing.RawUsage(input_tokens=14, output_tokens=6, cache_creation_tokens=2000, **{field: 800})
    assert "cache_creation_1h_input_tokens" not in billing.Eligibility.model_fields
