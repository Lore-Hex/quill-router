"""Property tests for the storage codec and the money-splitting arithmetic.

Three laws over pure functions that sit under durable state. Each one is cheap
to state and expensive to get wrong, because a violation is silent: nothing
raises, a row is simply wrong forever.

    codec round-trip   for every storage dataclass x,  C(**loads(json_body(x))) == x
    strict JSON        json_body(x) parses under a strict parser (no NaN/Infinity)
    conservation       sum(distribute_credit_amount(a, n)) == a  exactly, for all a, n

The codec law matters because `json_body` ELIDES fields whose value equals
their declared default:

    if field_value is None and field.default is None:            data.pop(...)
    elif field_value == [] and field.default_factory is list:    data.pop(...)
    elif field_value == {} and field.default_factory is dict:    data.pop(...)

That is only safe if the constructor rebuilds an equal value from nothing — and
`__post_init__` coercions can quietly break the equality. A field that elides
but does not reconstruct silently mutates a durable body that reconciliation,
ClickHouse ingestion and billing records all read back. Today's decode path
maps constructor failure to `None`, so such a break surfaces as unexplained
parity-sample shrinkage rather than an error, which is the worst possible way
to learn about it.

The strict-JSON clause covers a latent hole rather than a live one: `json.dumps`
runs with `allow_nan=True`, so a NaN metric would emit a bare `NaN` token that
is not valid JSON. No current writer can produce one — denominators are clamped
— so this is a guard against the field becoming reachable, and it is stated
here so the reasoning is not lost.

Conservation is the cheapest of the three and guards the most direct harm: a
split that does not sum to the whole either part-pays a refund (destroying
customer value) or over-credits.
"""

from __future__ import annotations

import dataclasses
import json
import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.storage_codec import json_body
from trusted_router.storage_gcp_counters import UNSHARDED, distribute_credit_amount
from trusted_router.storage_models import (
    ApiKey,
    Generation,
    ProviderBenchmarkSample,
    SyntheticProbeSample,
)
from trusted_router.types import UsageType

# ---------------------------------------------------------------------------
# Codec round-trip.
# ---------------------------------------------------------------------------


def _reject_constant(value: str) -> Any:  # pragma: no cover - raises by design
    raise AssertionError(f"json_body emitted the non-JSON constant {value!r}")


def _strict_loads(blob: str) -> Any:
    """Parse rejecting NaN/Infinity, which json.dumps emits by default."""
    return json.loads(blob, parse_constant=_reject_constant)


def _round_trips(instance: Any) -> None:
    blob = json_body(instance)
    decoded = _strict_loads(blob)
    rebuilt = type(instance)(**decoded)
    assert rebuilt == instance, (
        f"{type(instance).__name__} did not survive the codec.\n"
        f"  before: {instance}\n  after:  {rebuilt}\n  body:   {blob}"
    )


@st.composite
def generations(draw: Any) -> Generation:
    return Generation(
        id=draw(st.text(max_size=8)),
        request_id=draw(st.text(max_size=8)),
        workspace_id=draw(st.text(max_size=8)),
        key_hash=draw(st.text(max_size=8)),
        model=draw(st.text(max_size=8)),
        provider_name=draw(st.text(max_size=8)),
        app=draw(st.text(max_size=8)),
        tokens_prompt=draw(st.integers(min_value=0, max_value=10**7)),
        tokens_completion=draw(st.integers(min_value=0, max_value=10**7)),
        total_cost_microdollars=draw(st.integers(min_value=-(10**9), max_value=10**9)),
        usage_type=draw(st.sampled_from(list(UsageType))),
        speed_tokens_per_second=draw(
            st.floats(allow_nan=False, allow_infinity=False, width=32)
        ),
        finish_reason=draw(st.text(max_size=8)),
        status=draw(st.text(max_size=8)),
        streamed=draw(st.booleans()),
        usage_estimated=draw(st.booleans()),
        cached_input_tokens=draw(st.integers(min_value=0, max_value=10**6)),
        reasoning_tokens=draw(st.integers(min_value=0, max_value=10**6)),
        # Exercise both the elided (None / [] / {}) and populated branches.
        tool_calls=draw(st.one_of(st.none(), st.just([{"a": "b"}]))),
        provider=draw(st.one_of(st.none(), st.text(max_size=6))),
        elapsed_milliseconds=draw(st.one_of(st.none(), st.integers(0, 10**6))),
        region=draw(st.one_of(st.none(), st.text(max_size=6))),
        user=draw(st.one_of(st.none(), st.text(max_size=6))),
        session_id=draw(st.one_of(st.none(), st.text(max_size=6))),
        http_referer=draw(st.one_of(st.none(), st.text(max_size=6))),
        app_categories=draw(st.lists(st.text(max_size=4), max_size=2)),
        tags=draw(st.dictionaries(st.text(max_size=4), st.text(max_size=4), max_size=2)),
        operator_cost_microdollars=draw(st.one_of(st.none(), st.integers(0, 10**6))),
        route_type=draw(st.one_of(st.none(), st.text(max_size=6))),
    )


@given(generation=generations())
@settings(max_examples=400)
def test_generation_survives_the_codec(generation: Generation) -> None:
    """Generation carries billing-adjacent bodies and has the most fields with
    elidable defaults, so it is the class most likely to break the law."""
    _round_trips(generation)


@given(
    provider=st.text(max_size=8),
    model=st.text(max_size=8),
    tokens=st.integers(min_value=0, max_value=10**6),
    cost=st.integers(min_value=-(10**6), max_value=10**6),
    speed=st.floats(allow_nan=False, allow_infinity=False, width=32),
    streamed=st.booleans(),
)
@settings(max_examples=300)
def test_benchmark_sample_survives_the_codec(
    provider: str, model: str, tokens: int, cost: int, speed: float, streamed: bool
) -> None:
    _round_trips(
        ProviderBenchmarkSample(
            id="b",
            provider=provider,
            model=model,
            provider_name=provider,
            status="ok",
            usage_type=UsageType.CREDITS,
            source="synthetic",
            streamed=streamed,
            input_tokens=tokens,
            output_tokens=tokens,
            total_cost_microdollars=cost,
            speed_tokens_per_second=speed,
        )
    )


@given(
    text=st.text(max_size=10),
    creator=st.one_of(st.none(), st.text(max_size=8)),
    limit=st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
    disabled=st.booleans(),
)
@settings(max_examples=200)
def test_api_key_survives_the_codec(
    text: str, creator: str | None, limit: int | None, disabled: bool
) -> None:
    _round_trips(
        ApiKey(
            hash=text,
            salt=text,
            secret_hash=text,
            lookup_hash=text,
            name=text,
            label=text,
            workspace_id=text,
            creator_user_id=creator,
            disabled=disabled,
            limit_microdollars=limit,
        )
    )


@given(generation=generations())
@settings(max_examples=300)
def test_codec_output_is_strict_json(generation: Generation) -> None:
    """json.dumps runs with allow_nan=True, so a NaN would emit a bare `NaN`
    token that no strict JSON parser accepts. Unreachable today; pinned so it
    stays that way."""
    _strict_loads(json_body(generation))


def test_a_nan_metric_would_produce_invalid_json_today() -> None:
    """Records the latent hole precisely rather than asserting it away.

    No current writer can produce a NaN here — denominators are clamped before
    the division that computes this field — so this is a guard on the
    *reachability* of the field, not a live defect. If json_body ever gains
    allow_nan=False, this test flips and should be deleted along with the note.
    """
    generation = Generation(
        id="g", request_id="r", workspace_id="w", key_hash="k", model="m",
        provider_name="p", app="a", tokens_prompt=0, tokens_completion=0,
        total_cost_microdollars=0, usage_type=UsageType.CREDITS,
        speed_tokens_per_second=math.nan, finish_reason="stop", status="ok",
        streamed=False,
    )
    blob = json_body(generation)
    assert "NaN" in blob, "json_body no longer emits NaN; tighten this test"
    with pytest.raises(AssertionError, match="non-JSON constant"):
        _strict_loads(blob)


@given(generation=generations())
@settings(max_examples=200)
def test_elision_only_removes_fields_the_constructor_rebuilds(
    generation: Generation,
) -> None:
    """The elision rule is only sound if an absent field reconstructs equal.

    Stated separately from the round-trip so a failure says *which* field lied
    about its default rather than just that the object changed.
    """
    decoded = _strict_loads(json_body(generation))
    for field in dataclasses.fields(generation):
        if field.name in decoded:
            continue
        rebuilt = type(generation)(**{**decoded, field.name: getattr(generation, field.name)})
        assert rebuilt == type(generation)(**decoded), (
            f"field {field.name!r} was elided but does not reconstruct to an equal value"
        )


def test_every_codec_class_is_covered_here() -> None:
    """A new dataclass reaching json_body without a round-trip test is the way
    this law rots. The set is small and grep-able; keep it current."""
    covered = {Generation, ProviderBenchmarkSample, ApiKey, SyntheticProbeSample}
    for cls in covered:
        assert dataclasses.is_dataclass(cls), cls
    # Structural precondition: the law only holds for classes whose fields are
    # JSON-native. A nested dataclass or tuple field would decode as dict/list
    # and break equality, so it must fail here rather than silently in prod.
    for cls in covered:
        for field in dataclasses.fields(cls):
            annotation = str(field.type)
            assert "tuple" not in annotation.lower(), (
                f"{cls.__name__}.{field.name} is tuple-typed; asdict+json decodes it "
                "as a list and the round-trip law no longer holds"
            )


# ---------------------------------------------------------------------------
# Money splitting: conservation.
# ---------------------------------------------------------------------------


@given(
    amount=st.integers(min_value=-(2**62), max_value=2**62),
    shard_count=st.integers(min_value=1, max_value=64),
)
@settings(max_examples=1000)
def test_a_split_conserves_the_amount_exactly(amount: int, shard_count: int) -> None:
    """The whole point: no microdollar is created or destroyed by splitting.

    A violation part-pays a refund (destroying customer value) or over-credits.
    Quantified over negatives too, since refunds take the same path.
    """
    parts = distribute_credit_amount(amount, shard_count)
    assert len(parts) == shard_count
    assert sum(parts) == amount, f"split of {amount} over {shard_count} lost/created value"


@given(
    amount=st.integers(min_value=-(2**62), max_value=2**62),
    shard_count=st.integers(min_value=1, max_value=64),
)
@settings(max_examples=500)
def test_every_part_shares_the_sign_of_the_whole(amount: int, shard_count: int) -> None:
    """No part may point the other way. A positive shard inside a refund would
    credit a workspace during a debit."""
    for part in distribute_credit_amount(amount, shard_count):
        assert part == 0 or (part > 0) == (amount > 0)


@given(
    amount=st.integers(min_value=-(2**62), max_value=2**62),
    shard_count=st.integers(min_value=1, max_value=64),
)
@settings(max_examples=500)
def test_the_remainder_lands_on_exactly_one_shard(amount: int, shard_count: int) -> None:
    """Parts differ by at most one unit, and only the unsharded slot absorbs the
    remainder — otherwise a repeated split would drift."""
    parts = distribute_credit_amount(amount, shard_count)
    per_shard, remainder = divmod(abs(amount), shard_count)
    sign = -1 if amount < 0 else 1

    others = [p for i, p in enumerate(parts) if i != UNSHARDED]
    assert all(p == sign * per_shard for p in others), (
        "every non-remainder shard must carry exactly the even share"
    )
    assert parts[UNSHARDED] == sign * (per_shard + remainder), (
        "the remainder must land entirely on the unsharded slot"
    )


@given(shard_count=st.integers(min_value=1, max_value=64))
def test_splitting_zero_moves_nothing(shard_count: int) -> None:
    assert distribute_credit_amount(0, shard_count) == tuple([0] * shard_count)


@given(
    amount=st.integers(min_value=-(2**62), max_value=2**62),
    shard_count=st.integers(min_value=1, max_value=64),
)
@settings(max_examples=300)
def test_a_split_is_reversible(amount: int, shard_count: int) -> None:
    """Splitting the negation negates each part. Refund paths rely on this: a
    refund must undo exactly the debit it reverses, shard for shard."""
    forward = distribute_credit_amount(amount, shard_count)
    backward = distribute_credit_amount(-amount, shard_count)
    assert backward == tuple(-p for p in forward)


@pytest.mark.parametrize("shard_count", [0, -1, -64])
def test_a_non_positive_shard_count_is_refused(shard_count: int) -> None:
    """Refusing beats returning an empty split, which would silently move no
    money while reporting success."""
    with pytest.raises(ValueError, match="positive integer"):
        distribute_credit_amount(100, shard_count)


@given(data=st.data())
@settings(max_examples=300)
def test_a_small_amount_spread_thin_still_conserves(data: Any) -> None:
    """The thin case: when |amount| < shard_count most parts are zero. That is
    correct, and conservation must still hold exactly.

    Generated as a dependent pair rather than filtered, so Hypothesis spends
    every example on the case instead of discarding most of them.
    """
    shard_count = data.draw(st.integers(min_value=2, max_value=64))
    amount = data.draw(st.integers(min_value=1, max_value=shard_count - 1))
    parts = distribute_credit_amount(amount, shard_count)
    assert sum(parts) == amount
    assert parts.count(0) == shard_count - 1
    assert parts[UNSHARDED] == amount
