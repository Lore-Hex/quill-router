from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_transcription import authorize, settle
from trusted_router import catalog
from trusted_router.transcription import MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS

MODEL = "mistralai/voxtral-small-24b"


@pytest.fixture(name="transcription_key")
def audio_key(client: TestClient, user_headers: dict[str, str]) -> str:
    return str(client.post("/v1/keys", headers=user_headers, json={"name": "audio-tokens"}).json()["data"]["hash"])


def token_authorize(client: TestClient, key: str, **changes: Any) -> Any:
    return authorize(client, key, model=MODEL, **{
        "estimated_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "provider": {"only": ["tinfoil"], "min_privacy": "confidential"},
        **changes,
    })


def test_token_audio_reserves_and_settles_exactly_once(client: TestClient, transcription_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    response = token_authorize(client, transcription_key)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["additional_cost_reservation_microdollars"] == 0
    assert auth["estimated_cost_microdollars"] > 0
    from dataclasses import replace
    endpoint = catalog.endpoints_for_model(MODEL)[0]
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, replace(endpoint,
        prompt_price_microdollars_per_million_tokens=90_000_000,
        completion_price_microdollars_per_million_tokens=90_000_000))
    body = settle(auth, actual_input_tokens=1000, actual_output_tokens=1000)
    result = client.post("/v1/internal/gateway/settle", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["cost_microdollars"] == 844
    replay = client.post("/v1/internal/gateway/settle", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["generation_id"] == result.json()["data"]["generation_id"]


@pytest.mark.parametrize("changes", [
    {"estimated_input_tokens": 0}, {"max_output_tokens": 1},
    {"estimated_input_tokens": MAX_INPUT_TOKENS + 1},
    {"max_output_tokens": MAX_OUTPUT_TOKENS + 1},
])
def test_token_audio_cannot_underreserve(client: TestClient, transcription_key: str, changes: dict[str, Any]) -> None:
    response = token_authorize(client, transcription_key, **changes)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("changes", [
    {"actual_input_tokens": 0}, {"actual_output_tokens": 0},
    {"actual_input_tokens": MAX_INPUT_TOKENS + 1},
    {"actual_output_tokens": MAX_OUTPUT_TOKENS + 1},
    {"additional_cost_microdollars": 1}, {"route_type": "chat.completions"},
    {"cache_read_input_tokens": 1}, {"cache_creation_input_tokens": 1},
])
def test_token_audio_rejects_usage_drift(client: TestClient, transcription_key: str, changes: dict[str, Any]) -> None:
    response = token_authorize(client, transcription_key)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    body = settle(auth, **{"actual_input_tokens": 10, "actual_output_tokens": 10, **changes})
    result = client.post("/v1/internal/gateway/settle", json=body)
    assert result.status_code == 400, result.text
    refunded = client.post("/v1/internal/gateway/refund", json={"authorization_id": auth["authorization_id"], "status_code": 502, "error": "transcription_provider_error", "elapsed_seconds": 1})
    assert refunded.status_code == 200, refunded.text
