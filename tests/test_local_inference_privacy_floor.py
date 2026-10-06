"""Local inference honours a privacy floor against the route it calls.

Control-plane inference runs only in local and test environments
(main._control_plane_inference_enabled). It calls the model's default
provider, so a floor must hold for that route, not just for some route of the
model: GLM 5.2's default route is Z.AI's own API, and a Confidential request
must never reach it even though Chutes serves GLM 5.2 Confidential.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.catalog import (
    PRIVACY_TIER_CONFIDENTIAL,
    PROVIDERS,
    endpoint_privacy_tier,
    endpoint_zero_data_retention,
)
from trusted_router.catalog_data import Model

GLM = "z-ai/glm-5.2"
REFUSED = "No route candidates match the requested provider filters"


@pytest.fixture
def glm_on_vendor_and_chutes(monkeypatch: pytest.MonkeyPatch) -> None:
    drop_routes(monkeypatch, GLM)
    serve_on_fixture_route(monkeypatch, GLM, "zai", author="zai")
    chutes = serve_on_fixture_route(monkeypatch, GLM, "chutes", author="zai")
    assert endpoint_privacy_tier(chutes) == PRIVACY_TIER_CONFIDENTIAL, "fixture"


def _chat(client: TestClient, headers: dict[str, str], model: str, **extra: Any) -> Any:
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}
    return client.post("/v1/chat/completions", headers=headers, json=body)


@pytest.mark.usefixtures("glm_on_vendor_and_chutes")
def test_chat_never_dispatches_a_confidential_request_to_the_vendor_route(
    client: TestClient, inference_headers: dict[str, str]
) -> None:
    assert _chat(client, inference_headers, GLM).status_code == 200
    refused = _chat(client, inference_headers, GLM, provider={"min_privacy": "confidential"})
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == REFUSED


@pytest.mark.usefixtures("glm_on_vendor_and_chutes")
def test_responses_never_dispatches_a_confidential_request_to_the_vendor_route(
    client: TestClient, inference_headers: dict[str, str]
) -> None:
    body = {"model": GLM, "input": "hello"}
    assert client.post("/v1/responses", headers=inference_headers, json=body).status_code == 200
    refused = client.post(
        "/v1/responses",
        headers=inference_headers,
        json={**body, "provider": {"min_privacy": "confidential"}},
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == REFUSED


def test_chat_dispatches_a_confidential_request_when_the_local_route_qualifies(
    client: TestClient, inference_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = "fixture/confidential-default-route"
    model = Model(
        id=model_id, name=model_id, provider="chutes", context_length=131_072, prepaid_available=True
    )
    route = serve_on_fixture_route(monkeypatch, model_id, "chutes", author="chutes", model=model)
    assert endpoint_privacy_tier(route) == PRIVACY_TIER_CONFIDENTIAL, "fixture"
    response = _chat(client, inference_headers, model_id, provider={"min_privacy": "confidential"})
    assert response.status_code == 200, response.text


def _zdr_on_prepaid_only(monkeypatch: pytest.MonkeyPatch, *, prepaid_available: bool) -> str:
    """One host whose prepaid route has ZDR and whose BYOK route does not.

    reserved_quota bills BYOK when the model has no prepaid route available, so
    that is the route a request without an explicit usage reaches.
    """
    monkeypatch.setitem(
        PROVIDERS,
        "novita",
        dataclasses.replace(
            PROVIDERS["novita"],
            prepaid_zero_data_retention=True,
            provider_zero_data_retention=False,
            supports_byok=True,
        ),
    )
    model_id = "fixture/zdr-on-prepaid-only"
    model = Model(
        id=model_id,
        name=model_id,
        provider="novita",
        context_length=131_072,
        prepaid_available=prepaid_available,
        byok_available=True,
    )
    prepaid = serve_on_fixture_route(monkeypatch, model_id, "novita", author="novita", model=model)
    byok = serve_on_fixture_route(
        monkeypatch, model_id, "novita", author="novita", model=model, usage_type="BYOK"
    )
    assert endpoint_zero_data_retention(prepaid) is True, "fixture"
    assert endpoint_zero_data_retention(byok) is not True, "fixture"
    return model_id


def test_a_floor_is_checked_on_the_byok_route_when_billing_resolves_byok(
    client: TestClient, inference_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = _zdr_on_prepaid_only(monkeypatch, prepaid_available=False)
    refused = _chat(client, inference_headers, model_id, provider={"min_privacy": "zdr"})
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == REFUSED
    refused = client.post(
        "/v1/responses",
        headers=inference_headers,
        json={"model": model_id, "input": "hello", "provider": {"min_privacy": "zdr"}},
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == REFUSED


def test_a_floor_is_checked_on_the_prepaid_route_when_billing_resolves_credits(
    client: TestClient, inference_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = _zdr_on_prepaid_only(monkeypatch, prepaid_available=True)
    response = _chat(client, inference_headers, model_id, provider={"min_privacy": "zdr"})
    assert response.status_code == 200, response.text
