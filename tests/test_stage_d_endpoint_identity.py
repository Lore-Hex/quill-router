"""Stage D bills a stream in its selected endpoint's identity, even after the
endpoint has left the catalog.

The heartbeat's running charge and cap check, the reaper's snapshot booking
and the generation the drain rebuilds all read the delivered usage by the
selected endpoint's provider slug and router model id
(provider_contracts.provider_input_excludes_cache). Those came from the
catalog, and fell back to the authorization's own fields when the endpoint
was no longer listed, as happens when a deploy retires an endpoint while a
stream is in flight or before its reap. The selected endpoint may be a
fallback candidate on another provider than the authorization names, so
that fallback could read a resold-Claude stream in the inclusive convention
and book 29,013 microdollars for the billing reviewer's 33,655 scenario.

Every endpoint id names its provider and model by its own shape,
``<model id>@<provider slug>/<tier>`` (every composer of an endpoint id builds
it so), and catalog.endpoint_provider_and_model reads it when the catalog no
longer can.
"""
# ruff: noqa: F811, F401 - the stream helpers of tests/test_resold_claude_cache_accounting.py
from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes
from tests.fixture_routes import bypass_catalog_caches
from tests.test_resold_claude_cache_accounting import (
    EXCLUSIVE_STREAM_CHARGE,
    INCLUSIVE_STREAM_CHARGE,
    STREAM_NOW,
    STREAM_USAGE,
    _seed_stream,
    _stream_heartbeat,
    _stream_route,
)
from trusted_router import catalog as catalog_module
from trusted_router.catalog import endpoint_for_id, endpoint_provider_and_model
from trusted_router.catalog_registry import MODEL_ENDPOINTS
from trusted_router.stage_d import canonical_pricing_snapshot, endpoint_pricing_document
from trusted_router.storage_gcp_authorize import (
    reap_expired_reservations_result,
    snapshot_generation,
)
from trusted_router.storage_gcp_request_records import read_gateway_authorization
from trusted_router.storage_gcp_stage_d import (
    normalized_delivered_prompt,
    selected_endpoint_identity,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

SRC = Path(__file__).resolve().parents[1] / "src" / "trusted_router"

# The resold-Claude stream of the billing reviewer's scenario, selected as a
# fallback candidate: the authorization names the requested model and its
# primary provider, both of which read the usage in the inclusive convention.
RESOLD_CLAUDE = ("anthropic/claude-sonnet-5", "amazon-bedrock", "us.anthropic.claude-sonnet-5")
REQUESTED_MODEL, REQUESTED_PROVIDER = "openai/gpt-oss-120b", "together"


def test_every_endpoint_id_names_its_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id's shape alone gives the catalog's answer for every endpoint."""
    assert len(MODEL_ENDPOINTS) > 1_000
    for endpoint in MODEL_ENDPOINTS.values():
        assert endpoint_provider_and_model(endpoint.id) == (endpoint.provider, endpoint.model_id)
    # With the catalog unable to answer, the shape answers the same.
    monkeypatch.setattr(catalog_module, "endpoint_for_id", lambda _endpoint_id: None)
    for endpoint in MODEL_ENDPOINTS.values():
        assert endpoint_provider_and_model(endpoint.id) == (endpoint.provider, endpoint.model_id)


@pytest.mark.parametrize(
    "endpoint_id",
    [None, "", "anthropic/claude-x", "anthropic/claude-x@", "@amazon-bedrock/prepaid",
     "anthropic/claude-x@amazon-bedrock", "anthropic/claude-x@/prepaid", "anthropic/claude-x@amazon-bedrock/"],
)
def test_an_id_of_neither_kind_is_none(endpoint_id: str | None) -> None:
    assert endpoint_for_id(endpoint_id) is None
    assert endpoint_provider_and_model(endpoint_id) is None
    authorization = _fallback_authorization(endpoint_id or "")
    assert selected_endpoint_identity(authorization, endpoint_id) == (REQUESTED_PROVIDER, REQUESTED_MODEL)


def test_a_model_id_may_carry_an_at_sign() -> None:
    """The last separator is the endpoint's; earlier ones belong to the model."""
    assert endpoint_provider_and_model("vendor/model@2026-11@some-host/prepaid") == ("some-host", "vendor/model@2026-11")


def _fallback_authorization(endpoint_id: str, *, pricing_snapshot: str | None = None, prompt_tokens: int = 102_000) -> GatewayAuthorization:
    return GatewayAuthorization(
        id="gwa-resold-claude-stream", workspace_id="workspace", key_hash="key",
        model_id=REQUESTED_MODEL, provider=REQUESTED_PROVIDER, usage_type=UsageType.CREDITS,
        estimated_microdollars=40_000, credit_reservation_id="reservation", settled=False,
        endpoint_id=endpoint_id, candidate_endpoint_ids=[endpoint_id],
        pricing_snapshot=pricing_snapshot,
        heartbeat_seq=0, stage_d_reason="ok", stage_d_prompt_tokens=prompt_tokens, stage_d_max_output_tokens=500,
    )


def _retire(monkeypatch: pytest.MonkeyPatch, endpoint_id: str) -> None:
    """The endpoint leaves the catalog, as a deploy retiring it would make it."""
    bypass_catalog_caches(monkeypatch)
    monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    assert endpoint_for_id(endpoint_id) is None


def test_the_reaper_books_a_retired_endpoints_stream_in_its_own_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heartbeat while the endpoint is listed, the endpoint retired, then
    the reap: the booking, the finalized cost and the generation's prompt
    total are the resold-Claude figures, not the requested model's."""
    route = _stream_route(monkeypatch, *RESOLD_CLAUDE)
    authorization = _fallback_authorization(
        route.id, pricing_snapshot=canonical_pricing_snapshot(endpoint_pricing_document((route,))))
    db = FakeSpannerDatabase(now=STREAM_NOW)
    _seed_stream(db, authorization)
    heartbeat = _stream_heartbeat(db, route)
    assert heartbeat.accepted and heartbeat.running_micro == EXCLUSIVE_STREAM_CHARGE == 33_655

    _retire(monkeypatch, route.id)
    result = reap_expired_reservations_result(
        db, _ParamTypes, now=STREAM_NOW + timedelta(seconds=301), snapshot_booking_enabled=True)
    assert result.count == 1 and result.snapshot_bookings == 1
    assert db.reservations["reservation"]["actual_micro"] == EXCLUSIVE_STREAM_CHARGE
    stored = db.gateway_authorizations[authorization.id]
    assert stored["finalization_outcome"] == "reaped_snapshot"
    assert stored["finalized_cost_microdollars"] == EXCLUSIVE_STREAM_CHARGE
    (record,) = db.generation_records.values()
    generation = json.loads(record["payload"])
    assert generation["tokens_prompt"] == 102_000
    assert generation["total_cost_microdollars"] == EXCLUSIVE_STREAM_CHARGE
    with db.snapshot() as reader:
        reread = read_gateway_authorization(reader, _ParamTypes, authorization.id)
    assert reread is not None and endpoint_for_id(reread.selected_endpoint_id) is None
    rebuilt = snapshot_generation(reread, actual_micro=EXCLUSIVE_STREAM_CHARGE)
    assert rebuilt is not None and rebuilt.tokens_prompt == 102_000
    # What the authorization's own fields read the same usage as.
    assert normalized_delivered_prompt(REQUESTED_PROVIDER, STREAM_USAGE, model_id=REQUESTED_MODEL) == (0, 2_000)
    assert INCLUSIVE_STREAM_CHARGE == 29_013


def test_a_heartbeat_after_the_endpoint_retired_prices_and_caps_by_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first heartbeat arrives after the endpoint left the catalog: the
    running charge is the resold-Claude figure, and a 2,000-token prompt cap
    refuses the 102,000-token prompt as it does while the endpoint is listed."""
    route = _stream_route(monkeypatch, *RESOLD_CLAUDE)
    snapshot = canonical_pricing_snapshot(endpoint_pricing_document((route,)))
    _retire(monkeypatch, route.id)

    db = FakeSpannerDatabase(now=STREAM_NOW)
    _seed_stream(db, _fallback_authorization(route.id, pricing_snapshot=snapshot))
    heartbeat = _stream_heartbeat(db, route)
    assert heartbeat.accepted and heartbeat.running_micro == EXCLUSIVE_STREAM_CHARGE

    capped = FakeSpannerDatabase(now=STREAM_NOW)
    _seed_stream(capped, _fallback_authorization(route.id, pricing_snapshot=snapshot, prompt_tokens=2_000))
    refused = _stream_heartbeat(capped, route)
    assert not refused.accepted and refused.reason == "usage_exceeds_cap"


def test_stage_d_reads_the_authorizations_identity_only_as_the_last_resort() -> None:
    """Every Stage D reader of the stream's provider or model goes through
    selected_endpoint_identity: no other function in the heartbeat module,
    nor the reaper's booking or the rebuilt generation, reads
    ``authorization.provider`` or ``authorization.model_id``, or resolves
    the endpoint itself (``endpoint_for_id``, ``endpoint_provider_and_model``);
    and those two functions do call the helper."""
    offenders: list[str] = []
    callers: set[str] = set()
    for module, functions in (
        ("storage_gcp_stage_d.py", None),
        ("storage_gcp_authorize.py", {"snapshot_generation", "_finalize_reaped_reservation_atomic"}),
    ):
        tree = ast.parse((SRC / module).read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef) or function.name == "selected_endpoint_identity":
                continue
            if functions is not None and function.name not in functions:
                continue
            for node in ast.walk(function):
                if (isinstance(node, ast.Attribute) and node.attr in {"provider", "model_id"}
                        and isinstance(node.value, ast.Name) and node.value.id == "authorization"):
                    offenders.append(f"{module}:{function.name}:{node.lineno}")
                if isinstance(node, ast.Call):
                    callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if callee in {"endpoint_for_id", "endpoint_provider_and_model"}:
                        offenders.append(f"{module}:{function.name}:{node.lineno}:{callee}")
                    if callee == "selected_endpoint_identity":
                        callers.add(function.name)
    assert offenders == []
    assert {"snapshot_generation", "_finalize_reaped_reservation_atomic"} <= callers
