import copy
import json

from tests.test_async_settle_handler import env, prepare  # noqa: F401
from tests.test_async_settle_oracle import endpoint_from_candidate
from tests.test_async_settle_shadow import FIXTURE, NOW, signer, wire
from tests.test_settle_outbox_drain import _client, _typed_credit, _typed_key
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import validate_sample
from trusted_router.catalog_data import Model
from trusted_router.routes.internal import gateway
from trusted_router.services.async_settle_shadow import Runtime
from trusted_router.storage_models import generation_id_for_authorization


def test_actual_legacy_nonstream_refund_records_usage_missing(env, monkeypatch):  # noqa: F811 - imported fixture
    body, auth, key = prepare(env, kind="refund")
    _, db, rt, cfg = env
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg.release = "a" * 40
    cfg._async_settle_shadow_workspace_ids = frozenset({"ws-v1"})
    payload = json.loads(db.gateway_authorizations[auth.id]["payload"])
    payload["invocation_nonce"] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]["payload"] = json.dumps(payload)
    endpoints = {
        c["endpoint_id"]: endpoint_from_candidate(c) for c in body["billing_snapshot"]["candidates"]
    }
    for endpoint in endpoints.values():
        monkeypatch.setitem(
            gateway.MODELS,
            endpoint.model_id,
            Model(
                id=endpoint.model_id,
                name="shadow",
                provider=endpoint.provider,
                context_length=1000000,
                prepaid_available=True,
            ),
        )
    monkeypatch.setattr(gateway, "endpoint_for_id", endpoints.get)
    claims = verify_binding(FIXTURE["billing_shadow_binding"], [signer().trusted], NOW).model_dump()
    claims.update(
        authorization_id=auth.id,
        generation_id=generation_id_for_authorization(auth.id),
        key_id=auth.key_hash,
        invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id,
        snapshot_hash=body["terminal"]["snapshot_hash"],
    )
    envelope = copy.deepcopy(FIXTURE)
    envelope.update(
        billing_snapshot=body["billing_snapshot"],
        billing_shadow_binding=signer().sign(claims, NOW),
        raw_usage=None,
        terminal=None,
        payload_hash=None,
        go_error="usage_missing",
    )
    legacy = dict(
        authorization_id=auth.id,
        error_status=502,
        error_type="provider_error",
        elapsed_seconds=0.001,
        streamed=True,
        selected_model=auth.model_id,
        selected_endpoint=auth.endpoint_id,
        app="attested-gateway",
        route_type="chat.completions",
    )
    evidence = []

    class Store:
        def reserve(self, *args):
            return 100

        def booking(self, identity, deadline):
            row = db.gateway_authorizations[identity]
            return Booking(
                row["finalized_cost_microdollars"], row["finalization_outcome"], row["settled"]
            )

        def insert_sample(self, identity, row, deadline):
            validate_sample(row, identity)
            evidence.append(row)
            return "inserted"

        def flush(self, *args):
            pass

    shadow = Runtime(cfg, rt, Store())
    shadow.signer = signer()
    client = _client(cfg)
    client.app.state.async_settle_shadow = shadow
    try:
        response = client.post(
            "/v1/internal/gateway/refund",
            json=legacy,
            headers={"X-TR-Settlement-Shadow": wire(envelope)[0]},
        )
    finally:
        shadow.executor.shutdown()
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == 0
    assert _typed_credit(db, "ws-v1")["total_usage"] == _typed_key(db, key.hash)["usage"] == 0
    assert db.gateway_authorizations[auth.id]["finalization_outcome"] == "refunded"
    actual = [(r["classification"], r["reason_codes"]) for r in evidence]
    print("HTTP", response.status_code, "money refunded at zero; evidence", actual)
    assert actual == [("unevaluable", ["usage_missing"])]
