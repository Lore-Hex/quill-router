from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import configure_store
from trusted_router.strict_budget import strict_budget_slot


def test_strict_gateway_holds_refund_backpressure_and_no_regional_lease():
    store, db, _ = make_fake_store()
    workspace = store.create_workspace("owner", "strict", trial_credit_microdollars=1_000_000)
    _, key = store.create_api_key(
        workspace_id=workspace.id,
        creator_user_id="owner",
        name="strict",
        budget_strict=True,
        limit_daily_microdollars=1_000_000,
    )
    assert key.usage_shard_count == 1
    configure_store(store)
    client = TestClient(
        create_app(
            Settings(
                environment="test",
                regional_quota_leases_enabled=True,
                regional_quota_lease_issuance_enabled=True,
                regional_quota_lease_pilot_workspace_ids=workspace.id,
            ),
            configure_store_arg=False,
            init_observability=False,
        )
    )
    body = {
        "api_key_hash": key.hash,
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
    }
    first = client.post("/v1/internal/gateway/authorize", json=body)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization.settlement == "local"
    held = db.typed["tr_key_limit"][(key.hash, 0)]["reserved"]
    assert held > 0
    store.update_key(key.hash, {"limit_daily_microdollars": held})
    denied = client.post("/v1/internal/gateway/authorize", json=body)
    assert denied.status_code == 429, denied.text
    assert denied.headers["RateLimit-Remaining"] == "0"
    assert int(denied.headers["Retry-After"]) > 0
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == held
    with strict_budget_slot(key.hash):
        busy = client.post("/v1/internal/gateway/authorize", json=body)
    assert busy.status_code == 503, busy.text
    assert busy.headers["Retry-After"] == "1"
    refunded = client.post(
        "/v1/internal/gateway/refund", json={"authorization_id": data["authorization_id"]}
    )
    assert refunded.status_code == 200, refunded.text
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 0
    assert client.post("/v1/internal/gateway/authorize", json=body).status_code == 200
