"""The hold prices at most HOLD_OUTPUT_TOKENS of the client's output budget
(authorize_candidates.hold_output_tokens), through the gateway authorize route.

A client that sent max_tokens 131,072 on a catalog model held the priciest
candidate's price of all 131,072 tokens, about 78 times the actual cost of a
typical request, and was refused for insufficient credits with a balance that
covered the request many times over. The 16,384-token figure is the hold now,
whichever output field carries the budget.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.authorize_candidates import HOLD_OUTPUT_TOKENS
from trusted_router.storage import STORE

MODEL = "z-ai/glm-5.3"


def _hold(client: TestClient, key_hash: str, field: str, budget: int) -> int:
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash, "model": MODEL, "estimated_input_tokens": 7_680,
            field: budget, "idempotency_key": f"hold-output-cap:{field}:{budget}",
        },
    )
    assert response.status_code == 200, response.text
    authorization = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
    assert authorization is not None
    assert response.json()["data"]["estimated_cost_microdollars"] == authorization.estimated_microdollars
    return authorization.estimated_microdollars


def test_a_max_tokens_of_131072_holds_the_16384_figure(client: TestClient, user_headers: dict[str, str]) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "hold output cap"}).json()
    key_hash = created["data"]["hash"]
    bound = _hold(client, key_hash, "max_tokens", HOLD_OUTPUT_TOKENS)
    assert _hold(client, key_hash, "max_tokens", 131_072) == bound
    assert _hold(client, key_hash, "max_completion_tokens", 131_072) == bound
    assert _hold(client, key_hash, "max_output_tokens", 131_072) == bound
    assert _hold(client, key_hash, "max_tokens", HOLD_OUTPUT_TOKENS - 1) < bound
