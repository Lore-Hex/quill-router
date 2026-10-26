"""An authorization carries two figures (authorize_candidates.hold_output_tokens):
the estimate, the spend ceiling at the whole output budget, and the
reservation, the credit hold at most HOLD_OUTPUT_TOKENS of it.

A client sending the model's whole output window as max_tokens held the
window's worst case per request. The reservation is now the 16,384-token
figure, whichever output field carries the budget, while the authorization
row keeps the whole-budget estimate for the readers that need a ceiling
(Stage D, video and user-model settles).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.authorize_candidates import HOLD_OUTPUT_TOKENS
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, effective_endpoint
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, Workspace
from trusted_router.types import UsageType

MODEL = "z-ai/glm-5.3"
VIDEO_MODEL = "bytedance/seedance-2.5"
VIDEO_ENDPOINT = f"{VIDEO_MODEL}@venice/prepaid"


def _figures(client: TestClient, key_hash: str, workspace_id: str, field: str, budget: int) -> tuple[int, int]:
    """(the authorization's estimate, what the workspace's reserved counter grew by)."""
    reserved_before = STORE.credit_money[workspace_id].reserved_microdollars
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash, "model": MODEL, "estimated_input_tokens": 8_000,
            field: budget, "idempotency_key": f"hold-output-cap:{field}:{budget}",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    authorization = STORE.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert data["estimated_cost_microdollars"] == authorization.estimated_microdollars
    reserved = STORE.credit_money[workspace_id].reserved_microdollars - reserved_before
    return authorization.estimated_microdollars, reserved


def test_a_max_tokens_of_131072_reserves_the_16384_figure_and_estimates_the_whole_budget(
    client: TestClient, user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "hold output cap"}).json()
    key_hash, workspace_id = created["data"]["hash"], created["data"]["workspace_id"]
    at_the_bound, reserved_at_the_bound = _figures(client, key_hash, workspace_id, "max_tokens", HOLD_OUTPUT_TOKENS)
    assert reserved_at_the_bound == at_the_bound
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        estimate, reserved = _figures(client, key_hash, workspace_id, field, 131_072)
        assert reserved == at_the_bound
        assert estimate > at_the_bound
    below, reserved_below = _figures(client, key_hash, workspace_id, "max_tokens", HOLD_OUTPUT_TOKENS - 1)
    assert reserved_below == below < at_the_bound


@pytest.fixture
def token_video_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # The accounting contract, isolated from the provider's catalog state.
    monkeypatch.setitem(MODEL_ENDPOINTS, VIDEO_ENDPOINT, replace(
        MODEL_ENDPOINTS[VIDEO_ENDPOINT], completion_price_microdollars_per_million_tokens=10_700_000,
        published_completion_price_microdollars_per_million_tokens=10_700_000,
    ))


def test_a_token_billed_video_reserves_its_whole_budget_and_settles_past_the_bound(
    client: TestClient, inference_key: str, token_video_endpoint: None,
) -> None:
    """A video's settle is held to the authorization's estimate, so its
    reservation is the estimate: 80,000 video tokens at 10.70 per million
    reserve 856,000, and a 38,830-token completion settles at 415,481."""
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_lookup_hash": lookup_hash_api_key(inference_key), "model": VIDEO_MODEL,
        "estimated_input_tokens": 0, "max_output_tokens": 80_000, "route_type": "videos",
        "idempotency_key": "token-video-whole-budget", "request_fingerprint": "b" * 64,
        "provider": {"only": ["venice"]},
    })
    assert response.status_code == 200, response.text
    authorization_id = response.json()["data"]["authorization_id"]
    authorization = STORE.get_gateway_authorization(authorization_id)
    assert authorization is not None
    assert authorization.estimated_microdollars == 856_000
    # The key's fresh workspace holds nothing else: the whole budget is reserved.
    assert STORE.credit_money[authorization.workspace_id].reserved_microdollars == 856_000
    assert 856_000 > 10_700_000 * HOLD_OUTPUT_TOKENS // 1_000_000

    settle = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": authorization_id, "actual_input_tokens": 0, "actual_output_tokens": 38_830,
        "route_type": "videos", "selected_endpoint": VIDEO_ENDPOINT, "selected_model": VIDEO_MODEL,
        "finish_reason": "completed",
    })
    assert settle.status_code == 200, settle.text
    assert settle.json()["data"]["cost_microdollars"] == 415_481


def test_the_typed_store_holds_the_reservation_and_records_the_estimate() -> None:
    """The Spanner path: the credit counters and the reservation row's
    credit hold take the reservation; the key hold and the authorization
    row keep the estimate (a key cap counts outstanding spend ceilings)."""
    store, db = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="hold-cap-workspace", name="Hold cap", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": 1_000_000, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id, name="hold cap", creator_user_id=workspace.owner_user_id,
        limit_microdollars=1_000_000,
    )
    endpoint = effective_endpoint(
        next(endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.usage_type == "Credits")
    )
    model = MODELS[endpoint.model_id]
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace.id, key_hash=key.hash, estimate=262_244, reservation=32_868,
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS,
        model_id=model.id, provider=endpoint.provider, requested_model_id=model.id,
        candidate_model_ids=[model.id], region="us-central1", endpoint_id=endpoint.id,
        candidate_endpoint_ids=[endpoint.id], idempotency_key="hold-cap-typed",
        idempotency_fingerprint="f" * 64, expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert db.gateway_authorizations[authorization.id]["estimated_microdollars"] == 262_244
    reservation = db.reservations[authorization.credit_reservation_id]
    assert reservation["credit_reserved_micro"] == 32_868
    assert reservation["key_reserved_micro"] == 262_244
    assert db.typed[CREDIT_BALANCE_TABLE][(workspace.id, 0)]["reserved"] == 32_868


def _capped_key_store(**key_limits: int | bool):  # type: ignore[no-untyped-def]
    store, db = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="key-cap-workspace", name="Key cap", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": 10_000_000, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.create_api_key(workspace_id=workspace.id, creator_user_id=None, name="capped", **key_limits)
    return store, db, workspace, key


def _typed_authorize(store, workspace, key, *, estimate: int, reservation: int, **extra):  # type: ignore[no-untyped-def]
    endpoint = effective_endpoint(
        next(endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.usage_type == "Credits")
    )
    model = MODELS[endpoint.model_id]
    outcome, _authorization = store.authorize_gateway_typed(
        workspace_id=workspace.id, key_hash=key.hash, estimate=estimate, reservation=reservation,
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS,
        model_id=model.id, provider=endpoint.provider, requested_model_id=model.id,
        candidate_model_ids=[model.id], region="us-central1", endpoint_id=endpoint.id,
        candidate_endpoint_ids=[endpoint.id], idempotency_key=None,
        idempotency_fingerprint="f" * 64, expires_at=datetime.now(UTC) + timedelta(hours=2), **extra,
    )
    return outcome


# A 100-token prompt at 1 per million and a 131,072-token budget at 2 per
# million: the estimate is 262,244, the reservation 32,868. A key cap of
# 32,868 is a promise to the key's owner, and the estimate is what the
# request can spend: the cap refuses it, as it did when the hold was the
# estimate, while the workspace's credits are asked only for the reservation.
ESTIMATE, RESERVATION = 262_244, 32_868


def test_a_key_lifetime_cap_compares_the_estimate() -> None:
    store, db, workspace, key = _capped_key_store(limit_microdollars=RESERVATION)
    assert _typed_authorize(store, workspace, key, estimate=ESTIMATE, reservation=RESERVATION) == "key_limit_exceeded"
    assert db.typed[CREDIT_BALANCE_TABLE][(workspace.id, 0)]["reserved"] == 0
    assert not db.typed.get("tr_reservation", {})
    assert _typed_authorize(store, workspace, key, estimate=RESERVATION, reservation=RESERVATION) == "accepted"


@pytest.mark.parametrize("strict", [False, True])
def test_a_key_daily_window_compares_the_estimate(strict: bool) -> None:
    store, db, workspace, key = _capped_key_store(limit_daily_microdollars=RESERVATION, budget_strict=strict)
    limits = {"strict_budget": True} if strict else {"window_limits": {"daily": RESERVATION}}
    outcome = _typed_authorize(store, workspace, key, estimate=ESTIMATE, reservation=RESERVATION, **limits)
    assert outcome == "key_window_limit_exceeded:daily"
    assert db.typed[CREDIT_BALANCE_TABLE][(workspace.id, 0)]["reserved"] == 0
    assert not db.typed.get("tr_reservation", {})
    assert _typed_authorize(store, workspace, key, estimate=RESERVATION, reservation=RESERVATION, **limits) == "accepted"


def test_a_key_spend_limit_compares_the_estimate_on_the_legacy_path(
    client: TestClient, user_headers: dict[str, str],
) -> None:
    """The in-memory (legacy) path: the key limit sits between the
    reservation and the estimate, so the whole-budget request is refused
    for the key's limit and the at-the-bound one is admitted."""
    created = client.post("/v1/keys", headers=user_headers, json={"name": "capped", "limit": 0.5}).json()
    key_hash = created["data"]["hash"]

    def authorize(budget: int) -> int:
        response = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": key_hash, "model": MODEL, "estimated_input_tokens": 8_000, "max_tokens": budget,
            "idempotency_key": f"key-limit:{budget}",
        })
        return response.status_code

    assert authorize(HOLD_OUTPUT_TOKENS) == 200
    refused = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key_hash, "model": MODEL, "estimated_input_tokens": 8_000, "max_tokens": 131_072,
        "idempotency_key": "key-limit:131072",
    })
    assert refused.status_code == 402, refused.text
    assert refused.json()["error"]["type"] == "key_limit_exceeded"


def test_a_stream_with_max_tokens_131072_reserves_its_whole_budget_for_now(
    client: TestClient, user_headers: dict[str, str],
) -> None:
    """Until every plane runs the Stage D heartbeat reader that caps at the
    estimate, a stream's reservation is its estimate: the reader deployed
    before this change would cut a stream admitted with a lower one."""
    created = client.post("/v1/keys", headers=user_headers, json={"name": "stream hold"}).json()
    key_hash, workspace_id = created["data"]["hash"], created["data"]["workspace_id"]
    reserved_before = STORE.credit_money[workspace_id].reserved_microdollars
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key_hash, "model": MODEL, "estimated_input_tokens": 8_000, "max_tokens": 131_072,
        "stream": True, "idempotency_key": "stream-hold:131072",
    })
    assert response.status_code == 200, response.text
    authorization = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
    assert authorization is not None
    reserved = STORE.credit_money[workspace_id].reserved_microdollars - reserved_before
    assert reserved == authorization.estimated_microdollars
    at_the_bound, _reserved_at_the_bound = _figures(client, key_hash, workspace_id, "max_tokens", HOLD_OUTPUT_TOKENS)
    assert reserved > at_the_bound


def test_sharded_credit_headroom_is_judged_for_the_reservation() -> None:
    """Two credit shards of 16,434 each hold the 32,868 reservation between
    them: the cold-path headroom precheck and the shard rebalance consolidate
    for the credit hold, not for the 262,244 estimate."""
    from tests.test_credit_rebalance_contention import _seed, _set_credit_rows

    store, database, key = _seed([16_434, 16_434], workspace_id="sharded-workspace")
    _set_credit_rows(database, [16_434, 16_434], workspace_id="sharded-workspace")
    workspace = Workspace(id="sharded-workspace", name="Sharded", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    outcome = _typed_authorize(store, workspace, key, estimate=ESTIMATE, reservation=RESERVATION)
    assert outcome == "accepted", outcome
    reserved = sum(row["reserved"] for (ws, _shard), row in database.typed[CREDIT_BALANCE_TABLE].items() if ws == workspace.id)
    assert reserved == RESERVATION
