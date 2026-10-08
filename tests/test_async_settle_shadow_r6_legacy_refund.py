import copy
from dataclasses import replace

import pytest

from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Booking, compare
from trusted_router.schemas import GatewaySettleRequest


@pytest.mark.parametrize("original_streamed", [False, True])
def test_legacy_refund_placeholder_is_not_authorize_stream_identity(original_streamed):
    value = copy.deepcopy(FIXTURE)
    key = signer()
    ctx = context()
    claims = verify_binding(value["billing_shadow_binding"], [key.trusted], NOW).model_dump()
    claims["streamed"] = original_streamed
    value["billing_shadow_binding"] = key.sign(claims, NOW)
    value.update(raw_usage=None, terminal=None, payload_hash=None, go_error="usage_missing")
    value["observed"]["streamed"] = original_streamed
    # Exact fields sent by PR E 80de3a50 client.go:1223-1244. The unchanged
    # generic refund sender hardcodes streamed=true, also for nonstream calls.
    legacy = dict(
        authorization_id="auth-v1",
        error_status=502,
        error_type="provider_error",
        elapsed_seconds=0.001,
        streamed=True,
        selected_model=ctx.authorization.model_id,
        selected_endpoint=ctx.selected_endpoint,
        app="attested-gateway",
        route_type="chat.completions",
    )
    body = GatewaySettleRequest(**legacy)
    ctx = replace(ctx, body=body, attempted_kind="refund", booking=Booking(0, "refunded", True))
    result = compare(wire(value), ctx, [key.trusted])
    print(
        "original_streamed",
        original_streamed,
        "legacy_streamed",
        body.streamed,
        "classification",
        result.classification,
        "reasons",
        result.reasons,
        "booked",
        result.booked_micro,
    )
    assert (result.classification, result.reasons) == ("unevaluable", {"usage_missing"}), (
        "normal nonstream refund must not invent an identity mismatch"
    )


@pytest.mark.parametrize("damage", ["owner", "signature", "observed_stream"])
def test_refund_placeholder_keeps_proof_and_authorize_facts(damage):
    value = copy.deepcopy(FIXTURE)
    key = signer()
    claims = verify_binding(value["billing_shadow_binding"], [key.trusted], NOW).model_dump()
    if damage == "owner":
        claims["workspace_id"] = "foreign"
        value["billing_shadow_binding"] = key.sign(claims, NOW)
    elif damage == "signature":
        token = value["billing_shadow_binding"]
        head, payload, signature = token.split(".")
        value["billing_shadow_binding"] = head + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    else:
        value["observed"]["streamed"] = True
    value.update(raw_usage=None, terminal=None, payload_hash=None, go_error="usage_missing")
    body = GatewaySettleRequest(authorization_id="auth-v1", streamed=True, route_type="chat.completions")
    ctx = replace(context(), body=body, attempted_kind="refund", booking=Booking(0, "refunded", True))
    result = compare(wire(value), ctx, [key.trusted])
    assert result.classification == ("hash" if damage == "signature" else "identity")
