"""Real typed gateway with an in-memory Spanner database and unmodified catalog."""
from __future__ import annotations

import copy
import hashlib
import time
from typing import Any

import pytest

from tests.test_async_settle_admission import request
from tests.test_async_settle_shadow import FIXTURE, signer, wire
from tests.test_async_settle_ticket import runtime, settings
from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from tests.test_settle_outbox_drain import _client
from trusted_router import billing_snapshot as b
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.detached_jws import canonical
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services import async_settle
from trusted_router.services.async_settle_shadow import Runtime
from trusted_router.storage import configure_store
from trusted_router.synthetic.shadow_probe_contract import SHAPES, Shape


def authorize_body(shape: Shape, key_hash: str, payload: dict[str, Any]) -> GatewayAuthorizeRequest:
    # Enclave normalization: content/model/provider are routing inputs, while
    # the remaining public keys are the ordinary requested parameter names.
    return GatewayAuthorizeRequest(
        api_key_hash=key_hash, model=payload["model"], provider=payload.get("provider"),
        requested_parameters=["max_tokens"],
        metadata=payload.get("metadata"),
        max_output_tokens=8, estimated_input_tokens=10, stream=shape.stream,
        route_type="chat.completions" if shape.route == "/chat/completions" else "responses",
        invocation_nonce="probe-eligibility", idempotency_key=shape.name,
    )


@pytest.fixture(scope="module")
def probe_backend() -> Any:
    # One fake database and application per worker; every shape has a unique
    # idempotency key, fresh shadow runtime/counters and independent assertions.
    store, db, _ = _seed_typed_gateway_store()
    _, key = store.api_keys.create(workspace_id="ws-rpc", name="probe", creator_user_id="user-rpc",
                                  tags={"purpose": "synthetic_monitoring", "analytics": "excluded",
                                        "spend_control": "workspace_funding_only"})
    cfg = settings(async_settle_enabled=False, async_settle_shadow_workspaces="ws-rpc", release="a" * 40)
    client = _client(cfg)
    yield store, db, key, cfg, client
    client.close()


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_real_authorize_and_settle_compares_every_shape(shape: Shape, monkeypatch: pytest.MonkeyPatch, shadow_deadline_clock: Any, probe_backend: Any) -> None:
    store, db, key, cfg, client = probe_backend
    configure_store(store)
    # Independent records in the shared fake; never patch the STORE proxy.
    rt = runtime()
    rows: list[dict[str, Any]] = []

    class Evidence:
        def booking(self, identity: str, deadline: float) -> Booking:
            record = db.gateway_authorizations[identity]
            return Booking(record["finalized_cost_microdollars"], record["finalization_outcome"], True)

        def reserve(self, day: str, deadline: float) -> int:
            return 100

        def insert_sample(self, identity: str, row: dict[str, Any], deadline: float) -> str:
            rows.append(row)
            return "inserted"

        def flush(self, *args: Any) -> None:
            pass

    shadow = Runtime(cfg, rt, Evidence())
    shadow.signer = signer()
    req = request(rt)
    req.app.state.async_settle_shadow = shadow
    facts: list[b.Eligibility] = []
    original = async_settle.projection

    def projection(**kwargs: Any) -> dict[str, Any]:
        facts.append(kwargs["requested"])
        return original(**kwargs)

    monkeypatch.setattr(async_settle, "projection", projection)
    from trusted_router.storage_gcp import SpannerStore
    def unexpected_benchmark(*args: Any, **kwargs: Any) -> None:
        pytest.fail("cohort settlement entered provider benchmarks")
    monkeypatch.setattr(SpannerStore, "record_provider_benchmark", unexpected_benchmark)
    from trusted_router.storage_gcp_generations import SpannerGenerations
    original_benchmark = SpannerGenerations.benchmark_sample
    generations: list[Any] = []
    def benchmark_sample(generation: Any) -> Any:
        generations.append(generation)
        result = original_benchmark(generation)
        assert result is None
        return result
    monkeypatch.setattr(SpannerGenerations, "benchmark_sample", staticmethod(benchmark_sample))
    body = authorize_body(shape, key.hash, shape.body())
    try:
        data = gateway._authorize_gateway_sync(req, body, cfg)["data"]
        assert len(data["route_candidates"]) == 1
        assert len(facts) == 1
        assert facts[0] == b.Eligibility(route_type=body.route_type, streamed=shape.stream)
        claims = verify_binding(data["billing_shadow_binding"], [shadow.signer.trusted], int(time.time()))
        assert claims.settle_origin == "typed" and claims.billing_authority == "local"
        assert "billing_shadow_exclusion" not in data
        snapshot = b.BillingSnapshot.model_validate(data["billing_snapshot"])
        assert len(snapshot.candidates) == 1
        candidate = snapshot.candidates[0]
        assert candidate.provider == shape.provider and candidate.usage_type == "Credits"
        raw = b.RawUsage(input_tokens=10, output_tokens=2)
        evaluated = b.evaluate(snapshot, candidate.endpoint_id, raw, facts[0])
        envelope = copy.deepcopy(FIXTURE)
        envelope["billing_shadow_binding"] = data["billing_shadow_binding"]
        envelope["billing_snapshot"] = data["billing_snapshot"]
        envelope["raw_usage"] = raw.model_dump()
        envelope["observed"] = {"route_type": body.route_type, "streamed": shape.stream}
        terminal = envelope["terminal"]
        for name in ("authorization_id", "generation_id", "workspace_id", "key_id", "invocation_nonce",
                     "billing_authority", "journal_region", "epoch", "snapshot_version", "snapshot_hash",
                     "route_type", "streamed"):
            terminal[name] = getattr(claims, name)
        terminal.update(selected_endpoint=candidate.endpoint_id, usage=evaluated.usage.model_dump(),
                        charge_micro=evaluated.charge_micro)
        envelope["payload_hash"] = hashlib.sha256(canonical(terminal)).hexdigest()
        client.app.state.async_settle_shadow = shadow
        response = client.post("/v1/internal/gateway/settle", json={
            "authorization_id": data["authorization_id"], "actual_input_tokens": 10,
            "actual_output_tokens": 2, "selected_endpoint": candidate.endpoint_id,
            "route_type": body.route_type, "streamed": shape.stream, "usage_estimated": False,
        }, headers={"X-TR-Settlement-Shadow": wire(envelope)[0]})
        assert response.status_code == 200, response.text
        assert generations and all(g.synthetic and g.app == "TrustedRouter Synthetic" for g in generations)
        assert len(rows) == 1
        assert rows[0]["classification"] == "exact"
        assert rows[0]["adapter"] == shape.provider
        assert rows[0]["route_type"] == body.route_type
        assert rows[0]["streamed"] is shape.stream
        counters = shadow.counters.snapshot()[0][1]
        assert counters["samples_inserted"] == 1
        assert counters["comparison_dropped"] == 0
        assert counters["exclusions"] == counters["drops"] == []
    finally:
        shadow.executor.shutdown()


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("negative", ["monitor", "third_party"])
def test_real_authorize_negative_controls(negative: str) -> None:
    store, _, _ = _seed_typed_gateway_store()
    raw, key = store.api_keys.create(workspace_id="ws-rpc", name="probe", creator_user_id="user-rpc",
                                  tags={"purpose": "synthetic_monitoring", "analytics": "excluded",
                                        "spend_control": "workspace_funding_only"})
    cfg = settings(async_settle_enabled=False, async_settle_shadow_workspaces="ws-rpc")
    rt = runtime()
    shadow = Runtime(cfg, rt)
    shadow.signer = signer()
    req = request(rt)
    req.app.state.async_settle_shadow = shadow
    shape = SHAPES[0]
    payload = shape.body()
    if negative == "monitor":
        payload["model"] = "trustedrouter/monitor"
        del payload["provider"]
        cfg.async_settle_shadow_probe_api_key = raw
    else:
        candidate = next(e for e in MODEL_ENDPOINTS.values() if e.model_id.startswith("openai/")
                         and e.provider not in {"openai", "anthropic"} and e.usage_type == "Credits")
        payload.update(model=candidate.model_id, provider={"only": [candidate.provider], "allow_fallbacks": False})
    try:
        data = gateway._authorize_gateway_sync(req, authorize_body(shape, key.hash, payload), cfg)["data"]
        assert "billing_shadow_binding" not in data
        assert "billing_snapshot" not in data
        counter = shadow.counters.snapshot()[0][1]
        assert counter["samples_inserted"] == 0
    finally:
        shadow.executor.shutdown()


@pytest.mark.catalog_as_built
def test_metadata_still_receives_binding_but_is_not_a_probe_body(probe_backend: Any) -> None:
    store, _, key, cfg, _ = probe_backend
    configure_store(store)
    rt = runtime()
    shadow = Runtime(cfg, rt)
    shadow.signer = signer()
    req = request(rt)
    req.app.state.async_settle_shadow = shadow
    shape = SHAPES[0]
    payload = shape.body() | {"metadata": {"trustedrouter_synthetic": "true"}}
    body = authorize_body(shape, key.hash, payload)
    body.idempotency_key = "metadata-eligible"
    try:
        data = gateway._authorize_gateway_sync(req, body, cfg)["data"]
        assert data["billing_shadow_binding"] and data["billing_snapshot"]
        # Metadata is a schema-defined field, not a requested capability.
        # The cohort body is intentionally narrower than router eligibility.
        with pytest.raises(ValueError, match="fixed shape"):
            shape.validate_body(payload)
    finally:
        shadow.executor.shutdown()
