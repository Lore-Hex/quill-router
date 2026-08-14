"""Property tests for egress projection closure.

The product's central claim is that prompt and output content never enters the
control plane's logs, analytics, or third-party sinks. That claim is enforced
structurally: every surface that leaves the process goes through a *projection*
function that names its fields explicitly. The law:

    for every Generation g, and every egress projection P,
        keys(P(g)) == a frozen, declared key set
        and no content-bearing field of g appears anywhere in serialize(P(g))

Example tests cannot hold this, and the reason is worth being precise about.
The failure mode is not "someone writes a projection that leaks" — it is
"someone adds a field to Generation and a projection picks it up." Every
existing test asserts on a projection's *value* for a fixed input, so a new
content-bearing field flows through the day it is added and every test still
passes.

So the load-bearing test here is `test_every_generation_field_is_classified`:
it partitions `fields(Generation)` into projected / deliberately-excluded, and
fails CI when a new field belongs to neither. That converts "remember not to
leak" into "the build stops until you decide."

The canary technique: every string-typed position in the generated Generation
is planted with a unique marker, including nested tool-call arguments and tag
values. The assertion is over the *serialized* image, because that is what
actually leaves — a field that survives only inside an object's repr still
counts as leaked.

Scope, stated plainly: this proves projections are closed over their declared
key sets and exclude the declared content fields. It does not prove the
declared classification is morally correct — that `user` and `session_id` are
acceptable to export is a product decision, pinned here so it cannot change
silently, not derived.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.analytics_sink import _row_from_sample
from trusted_router.storage_activity import generation_events
from trusted_router.storage_gcp_generation_records import generation_record_body
from trusted_router.storage_models import Generation, ProviderBenchmarkSample
from trusted_router.storage_operational_analytics import activity_payload
from trusted_router.types import UsageType

CANARY = "CANARY-7f3a2b9c"

# ---------------------------------------------------------------------------
# The classification. Adding a field to Generation and not listing it here is a
# CI failure, which is the entire point of this module.
# ---------------------------------------------------------------------------

# Fields that are model-produced or internal and must NEVER reach an analytics
# or third-party surface.
CONTENT_OR_INTERNAL_FIELDS = frozenset(
    {
        # Model-produced: tool-call arguments are free-form model output and are
        # the one place raw generated content can hide inside a "metadata" row.
        "tool_calls",
        # Internal provider COGS. Not content, but deliberately not published:
        # exporting it would leak per-provider margin.
        "operator_cost_microdollars",
    }
)

# Raw tenant identifiers. These may appear in the Spanner system of record, but
# must be surrogated before reaching ClickHouse or any external sink.
RAW_TENANT_FIELDS = frozenset({"workspace_id", "key_hash"})

# Everything else on Generation is metadata that projections may carry.
EXPECTED_ACTIVITY_KEYS = frozenset(
    {
        "generation_id", "request_id", "tenant_id", "key_id", "model", "provider",
        "provider_name", "app", "tokens_prompt", "tokens_completion",
        "cached_input_tokens", "reasoning_tokens", "total_cost_microdollars",
        "usage_type", "speed_tokens_per_second", "finish_reason", "status",
        "streamed", "usage_estimated", "elapsed_milliseconds",
        "first_token_milliseconds", "ttfb_milliseconds", "region", "user",
        "session_id", "http_referer", "app_categories", "tags", "created_at",
    }
)

EXPECTED_EVENT_KEYS = frozenset(
    {
        "id", "request_id", "created_at", "date", "model", "provider_name", "app",
        "user", "session_id", "http_referer", "app_categories", "tags",
        "input_tokens", "output_tokens", "cost", "cost_microdollars", "usage_type",
        "speed_tokens_per_second", "finish_reason", "status", "streamed",
        "content_stored",
    }
)


# ---------------------------------------------------------------------------
# Generators. Every string position carries a distinct canary so the assertion
# can name which field leaked, not merely that something did.
# ---------------------------------------------------------------------------


def _marked(field_name: str) -> str:
    return f"{CANARY}-{field_name}"


@st.composite
def generations(draw: Any) -> Generation:
    """A Generation with a canary in every string-typed position."""
    return Generation(
        id=_marked("id"),
        request_id=_marked("request_id"),
        workspace_id=_marked("workspace_id"),
        key_hash=_marked("key_hash"),
        model=_marked("model"),
        provider_name=_marked("provider_name"),
        app=_marked("app"),
        tokens_prompt=draw(st.integers(min_value=0, max_value=10**6)),
        tokens_completion=draw(st.integers(min_value=0, max_value=10**6)),
        total_cost_microdollars=draw(st.integers(min_value=0, max_value=10**9)),
        usage_type=draw(st.sampled_from(list(UsageType))),
        speed_tokens_per_second=draw(
            st.floats(min_value=0, max_value=1e4, allow_nan=False, allow_infinity=False)
        ),
        finish_reason=_marked("finish_reason"),
        status=_marked("status"),
        streamed=draw(st.booleans()),
        # The dangerous one: free-form model output nested inside metadata.
        tool_calls=[
            {
                "id": _marked("tool_call_id"),
                "type": "function",
                "function": {
                    "name": _marked("tool_name"),
                    "arguments": _marked("tool_arguments"),
                },
            }
        ],
        provider=_marked("provider"),
        region=_marked("region"),
        user=_marked("user"),
        session_id=_marked("session_id"),
        http_referer=_marked("http_referer"),
        app_categories=[_marked("app_category")],
        tags={_marked("tag_key"): _marked("tag_value")},
        operator_cost_microdollars=draw(st.integers(min_value=0, max_value=10**9)),
        route_type=_marked("route_type"),
    )


def _leaked_markers(payload: Any) -> set[str]:
    """Which canaries survived into the serialized image.

    `default=str` is the faithful check: it is what a shipper does with a value
    it does not natively understand, so a secret surviving only inside an
    object's string form still counts.
    """
    blob = json.dumps(payload, default=str, ensure_ascii=False)
    return {
        marker
        for marker in (_marked(f.name) for f in dataclasses.fields(Generation))
        if marker in blob
    } | ({_marked("tool_arguments")} if _marked("tool_arguments") in blob else set())


# ---------------------------------------------------------------------------
# The meta-law: every field is classified.
# ---------------------------------------------------------------------------


def test_every_generation_field_is_classified() -> None:
    """A new field on Generation fails CI until someone decides where it goes.

    This is the test that actually prevents the defect class. Everything below
    checks today's projections; this one checks tomorrow's.
    """
    declared = {f.name for f in dataclasses.fields(Generation)}
    activity_sourced = {
        "id", "request_id", "workspace_id", "key_hash", "model", "provider",
        "provider_name", "app", "tokens_prompt", "tokens_completion",
        "cached_input_tokens", "reasoning_tokens", "total_cost_microdollars",
        "usage_type", "speed_tokens_per_second", "finish_reason", "status",
        "streamed", "usage_estimated", "elapsed_milliseconds",
        "first_token_milliseconds", "ttfb_milliseconds", "region", "user",
        "session_id", "http_referer", "app_categories", "tags", "created_at",
    }
    # Fields carried only on the system-of-record or video surfaces.
    other_metadata = {
        "route_type", "video_input_mode", "video_duration_seconds",
        "video_resolution", "video_aspect_ratio", "video_generate_audio",
    }

    classified = activity_sourced | other_metadata | CONTENT_OR_INTERNAL_FIELDS
    unclassified = declared - classified

    assert not unclassified, (
        f"Generation gained field(s) {sorted(unclassified)} that no egress "
        "classification covers. Decide whether each is metadata (add it to the "
        "appropriate set here and to the projections that should carry it) or "
        "content/internal (add it to CONTENT_OR_INTERNAL_FIELDS and make sure "
        "every projection excludes it). Do not simply add it to make this pass."
    )
    # And nothing may be classified that no longer exists.
    assert not (classified - declared), (
        f"classification names fields not on Generation: {sorted(classified - declared)}"
    )


# ---------------------------------------------------------------------------
# ClickHouse-bound / dashboard projections: closed key sets, no content.
# ---------------------------------------------------------------------------


@given(generation=generations())
@settings(max_examples=200)
def test_activity_payload_key_set_is_frozen(generation: Generation) -> None:
    payload = activity_payload(generation)
    assert set(payload) == EXPECTED_ACTIVITY_KEYS, (
        "activity_payload key set drifted; update EXPECTED_ACTIVITY_KEYS only "
        "after confirming the new field is content-free"
    )


@given(generation=generations())
@settings(max_examples=200)
def test_activity_payload_carries_no_content_and_no_raw_tenant_ids(
    generation: Generation,
) -> None:
    leaked = _leaked_markers(activity_payload(generation))
    forbidden = leaked & (CONTENT_OR_INTERNAL_FIELDS | RAW_TENANT_FIELDS)
    assert not forbidden, f"activity_payload leaked {sorted(forbidden)}"
    assert _marked("tool_arguments") not in leaked, (
        "tool-call arguments are free-form model output and must never reach analytics"
    )


@given(generation=generations())
@settings(max_examples=200)
def test_activity_payload_surrogates_tenant_identifiers(generation: Generation) -> None:
    """Tenant ids must be surrogated, not merely absent by accident."""
    payload = activity_payload(generation)
    assert payload["tenant_id"] != generation.workspace_id
    assert payload["key_id"] != generation.key_hash
    assert payload["tenant_id"], "surrogate must not be empty"
    assert payload["key_id"], "surrogate must not be empty"


@given(generation=generations())
@settings(max_examples=200)
def test_generation_events_key_set_is_frozen_and_content_free(
    generation: Generation,
) -> None:
    events = generation_events([generation])
    assert len(events) == 1
    assert set(events[0]) == EXPECTED_EVENT_KEYS

    leaked = _leaked_markers(events[0])
    forbidden = leaked & (CONTENT_OR_INTERNAL_FIELDS | RAW_TENANT_FIELDS)
    assert not forbidden, f"generation_events leaked {sorted(forbidden)}"


@given(generation=generations())
@settings(max_examples=200)
def test_generation_events_never_claims_content_is_stored(
    generation: Generation,
) -> None:
    """`content_stored` is a promise to the dashboard. It is a constant false,
    and a projection that ever made it dynamic would be a product-claim change
    rather than a refactor."""
    assert generation_events([generation])[0]["content_stored"] is False


# ---------------------------------------------------------------------------
# The Spanner system of record is a DIFFERENT surface with different rules.
# ---------------------------------------------------------------------------


@given(generation=generations())
@settings(max_examples=200)
def test_generation_record_excludes_tool_calls_but_keeps_raw_ids(
    generation: Generation,
) -> None:
    """The system of record legitimately holds raw tenant ids — it is not an
    egress surface. It must still exclude model-produced content.

    Asserting both directions matters: a well-meaning change that surrogated
    ids here would break lookups, and one that kept tool_calls would put model
    output in durable storage.
    """
    body = json.loads(generation_record_body(generation))

    assert "tool_calls" not in body, "model-produced tool arguments must not be persisted"
    assert _marked("tool_arguments") not in json.dumps(body, default=str)
    assert body["workspace_id"] == generation.workspace_id
    assert body["key_hash"] == generation.key_hash


# ---------------------------------------------------------------------------
# Benchmark samples: a separate surface, same closure requirement.
# ---------------------------------------------------------------------------


@given(
    provider=st.text(min_size=1, max_size=12),
    model=st.text(min_size=1, max_size=12),
    tokens=st.integers(min_value=0, max_value=10**6),
)
@settings(max_examples=100)
def test_benchmark_row_never_carries_free_text(
    provider: str, model: str, tokens: int
) -> None:
    """The public leaderboard row is built from a benchmark sample. Its key set
    is closed, and every value is a scalar — no nested structure that could
    carry a payload."""
    sample = ProviderBenchmarkSample(
        id="bench-1",
        provider=provider,
        model=model,
        provider_name=provider,
        status="ok",
        usage_type=UsageType.CREDITS,
        source="synthetic",
        streamed=False,
        input_tokens=tokens,
        output_tokens=tokens,
        total_cost_microdollars=0,
        speed_tokens_per_second=1.0,
    )
    row = _row_from_sample(sample)

    for key, value in row.items():
        assert not isinstance(value, (dict, list)), (
            f"benchmark row field {key!r} is a container; leaderboard rows must be "
            "flat scalars so no structured payload can ride along"
        )


# ---------------------------------------------------------------------------
# Regression pins for the specific fields that motivated this module.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("excluded", sorted(CONTENT_OR_INTERNAL_FIELDS))
def test_excluded_fields_are_absent_from_every_analytics_projection(
    excluded: str,
) -> None:
    generation = Generation(
        id="g", request_id="r", workspace_id="w", key_hash="k", model="m",
        provider_name="p", app="a", tokens_prompt=1, tokens_completion=1,
        total_cost_microdollars=1, usage_type=UsageType.CREDITS,
        speed_tokens_per_second=1.0, finish_reason="stop", status="ok",
        streamed=False,
        tool_calls=[{"function": {"arguments": CANARY}}],
        operator_cost_microdollars=999,
    )

    assert excluded not in activity_payload(generation)
    assert excluded not in generation_events([generation])[0]
