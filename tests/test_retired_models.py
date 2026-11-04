"""Retirement blocks new work without deleting historical money records."""

import pytest
from fastapi.testclient import TestClient

from trusted_router.custom_model_billing import owner_share_microdollars
from trusted_router.custom_model_markup_billing import custom_model_markup_owner_share_microdollars
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.types import UsageType
from trusted_router.user_model_rules import user_model_gateway_pair

IDS = [
    "tr-custom-model/alice-demo",
    "tr-user-model/alice-demo",
    "trustedrouter/user-demo",
    "nyte/user-demo",
    " TR-USER-MODEL/alice-demo ",
]


@pytest.mark.parametrize("value", [1, True, {}, "tr-user-model/demo"])
def test_retirement_check_does_not_crash_on_malformed_fallbacks(client, inference_headers, value):
    response = client.post("/v1/chat/completions", headers=inference_headers, json={
        "model": "anthropic/claude-sonnet-4.6", "models": value,
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("terminal", ["settle", "refund"])
def test_pre_retirement_custom_markup_holds_still_finalize_once(client, terminal):
    from trusted_router.catalog import MODELS, default_endpoint_for_model
    from trusted_router.routes.helpers import cost_microdollars

    owner = STORE.ensure_user("historical-creator@example.com")
    key = client.post("/v1/keys", headers={"x-trustedrouter-user": "payer@example.com"},
                      json={"name": "historical"}).json()["data"]
    payer = STORE.find_user_by_email("payer@example.com")
    workspace = STORE.list_workspaces_for_user(payer.id)[0]
    model = MODELS["anthropic/claude-sonnet-4.6"]
    endpoint = default_endpoint_for_model(model)
    assert endpoint is not None
    cost = cost_microdollars(endpoint, 100, 50)
    reservation = STORE.reserve(workspace.id, key["hash"], cost * 2)
    auth = STORE.create_gateway_authorization(
        workspace_id=workspace.id, key_hash=key["hash"], model_id=model.id,
        provider=endpoint.provider, usage_type=UsageType.CREDITS,
        estimated_microdollars=cost * 2, credit_reservation_id=reservation.id,
        key_reserved_microdollars=0, endpoint_id=endpoint.id,
        candidate_model_ids=[model.id], candidate_endpoint_ids=[endpoint.id],
        requested_model_id="tr-custom-model/retired", custom_model_id="tr-custom-model/retired",
        custom_model_revision=1, custom_model_markup_basis_points=10_000,
        custom_model_owner_user_id=owner.id,
    )
    body = {"authorization_id": auth.id}
    if terminal == "settle":
        body.update(actual_input_tokens=100, actual_output_tokens=50, elapsed_seconds=0.2)
    for _ in range(2):
        response = client.post(f"/v1/internal/gateway/{terminal}", json=body)
        assert response.status_code == 200, response.text
    expected = custom_model_markup_owner_share_microdollars(cost) if terminal == "settle" else 0
    assert STORE.earnings_summary(owner.id)["total_earned"] == expected
    assert len(STORE.list_credit_movements(f"user:{owner.id}")) == (1 if terminal == "settle" else 0)


@pytest.mark.parametrize("model_id", IDS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("path", ["chat/completions", "responses", "messages"])
def test_retired_inference_fails_before_dispatch(client, inference_headers, model_id, stream, path):
    response = client.post(
        f"/v1/{path}",
        headers=inference_headers,
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "hello"}],
            "input": "hello",
            "max_tokens": 8,
            "stream": stream,
        },
    )
    assert response.status_code == 410, response.text
    assert "retired" in response.text


@pytest.mark.parametrize("model_id", IDS)
@pytest.mark.parametrize("fallback", [False, True])
def test_gateway_rejects_retired_ids_before_reserving(
    client, user_headers, monkeypatch, model_id, fallback
):
    key = client.post("/v1/keys", headers=user_headers, json={"name": "retirement"}).json()["data"]

    def forbidden(*args, **kwargs):
        pytest.fail("Retired model looked up an endpoint or reserved credits")

    for method in ["get_custom_model", "get_user_model", "reserve", "create_gateway_authorization"]:
        monkeypatch.setattr(InMemoryStore, method, forbidden)
    body = {
        "api_key_hash": key["hash"],
        "model": model_id,
        "estimated_input_tokens": 10,
        "idempotency_key": "old-replay-key",
    }
    if fallback:
        body.update(model="anthropic/claude-sonnet-4.6", models=[model_id])
    response = client.post("/v1/internal/gateway/authorize", json=body)
    assert response.status_code == 410, response.text


@pytest.mark.parametrize(
    "path",
    [
        "/custom-models",
        "/user-models",
        "/v1/custom-models",
        "/v1/user-models",
        "/v1/user-models/tr-user-model/alice-demo/clock-in",
        "/v1/user-models/tr-user-model/alice-demo/heartbeat",
        "/v1/user-models/tr-user-model/alice-demo/rotate-secrets",
        "/v1/internal/gateway/resolve-custom-model",
        "/console/custom-models",
        "/console/user-models",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_retired_routes_are_unregistered(client, user_headers, path, method):
    response = client.request(method, path, headers=user_headers, json={"model": IDS[0]})
    assert response.status_code in {404, 405}, response.text


def test_retired_ui_docs_and_openapi_are_removed(client, user_headers):
    for path in ["/docs/custom-models", "/docs/user-models", "/user-chat?model=" + IDS[0]]:
        assert client.get(path).status_code == 404
    for path in ["/console", "/docs", "/sitemap.xml", "/sitemap-core.xml", "/openapi.json"]:
        page = client.get(path, headers=user_headers)
        assert page.status_code == 200, path
        for link in ["/custom-models", "/user-models", "resolve-custom-model"]:
            assert link not in page.text, (path, link)
    chat = client.get("/chat")
    assert chat.status_code == 200
    assert "lockedModelId" not in chat.text
    script = client.get("/static/chat.js")
    assert script.status_code == 200
    assert "LOCKED_MODEL" not in script.text


@pytest.mark.parametrize("terminal", ["settle", "refund"])
def test_pre_retirement_user_model_holds_still_finalize_once(client: TestClient, terminal: str):
    owner = STORE.ensure_user("legacy-owner@example.com")
    key = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "payer@example.com"},
        json={"name": "historical"},
    ).json()["data"]
    payer = STORE.find_user_by_email("payer@example.com")
    workspace = STORE.list_workspaces_for_user(payer.id)[0]
    model_id = "tr-user-model/legacy-removed"
    model, endpoint = user_model_gateway_pair(
        model_id=model_id,
        name="Historical model",
        revision=1,
        prompt_price_microdollars_per_m=2_000_000,
        completion_price_microdollars_per_m=3_000_000,
        owner_user_id=owner.id,
        upstream_model_id=model_id,
    )
    reservation = STORE.reserve(workspace.id, key["hash"], 20_000)
    # Simulate an already committed authorization, not a new public request.
    auth = STORE.create_gateway_authorization(
        workspace_id=workspace.id,
        key_hash=key["hash"],
        model_id=model.id,
        provider=model.provider,
        usage_type=UsageType.CREDITS,
        estimated_microdollars=20_000,
        credit_reservation_id=reservation.id,
        key_reserved_microdollars=0,
        requested_model_id=model_id,
        endpoint_id=endpoint.id,
        candidate_model_ids=[model.id],
        candidate_endpoint_ids=[endpoint.id],
        custom_model_id=model_id,
        custom_model_revision=1,
        user_provided_model_id=model_id,
        user_provided_model_revision=1,
        user_model_prompt_price_microdollars_per_m=2_000_000,
        user_model_completion_price_microdollars_per_m=3_000_000,
        user_model_owner_user_id=owner.id,
    )
    body = {"authorization_id": auth.id}
    if terminal == "settle":
        body.update(actual_input_tokens=1_000, actual_output_tokens=2_000, elapsed_seconds=0.2)
    for _ in range(2):
        response = client.post(f"/v1/internal/gateway/{terminal}", json=body)
        assert response.status_code == 200, response.text
    expected = owner_share_microdollars(8_000) if terminal == "settle" else 0
    assert STORE.earnings_summary(owner.id)["total_earned"] == expected
    movements = STORE.list_credit_movements(f"user:{owner.id}")
    assert len(movements) == (1 if terminal == "settle" else 0)
