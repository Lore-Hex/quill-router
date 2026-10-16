from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.transcription import TRANSCRIPTION_MODELS


@pytest.fixture
def transcription_key(client: TestClient, user_headers: dict[str, str]) -> str:
    return str(client.post("/v1/keys", headers=user_headers, json={"name": "transcription"}).json()["data"]["hash"])


def authorize(client: TestClient, key: str, **changes: Any) -> Any:
    return client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key,
        "model": "openai/whisper-1",
        "route_type": "audio.transcriptions",
        "estimated_input_tokens": 0,
        "max_output_tokens": 1,
        "transcription_duration_ms": 1250,
        "idempotency_key": "audio-one",
        "request_fingerprint": "b" * 64,
        **changes,
    })


def settle(auth: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "authorization_id": auth["authorization_id"], "request_id": "transcribed",
        "actual_input_tokens": 0, "actual_output_tokens": 0,
        "elapsed_seconds": 1, "route_type": "audio.transcriptions", "finish_reason": "stop",
        "additional_cost_microdollars": auth["additional_cost_reservation_microdollars"],
        **changes,
    }


def test_audio_quotes_are_not_text_tokens() -> None:
    assert TRANSCRIPTION_MODELS["openai/whisper-1"].quote(60_000) == 6330
    assert TRANSCRIPTION_MODELS["mistralai/voxtral-mini-2602"].quote(60_000) == 3165
    assert TRANSCRIPTION_MODELS["openai/whisper-large-v3-turbo"].quote(1) == 10_550
    assert TRANSCRIPTION_MODELS["openai/whisper-1"].quote(1001) == 211
    for bad in (0, -1, 600_001, True):
        with pytest.raises(ValueError):
            TRANSCRIPTION_MODELS["openai/whisper-1"].quote(bad)


@pytest.mark.parametrize("model", TRANSCRIPTION_MODELS)
def test_audio_frozen_quote_exactly_once(client: TestClient, transcription_key: str, model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    response = authorize(client, transcription_key, model=model)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    expected = TRANSCRIPTION_MODELS[model].quote(1250)
    assert auth["estimated_cost_microdollars"] == expected
    assert auth["additional_cost_reservation_microdollars"] == expected
    monkeypatch.setitem(TRANSCRIPTION_MODELS, model, replace(TRANSCRIPTION_MODELS[model], minute_cost_microdollars=90_000, request_cost_microdollars=90_000))
    result = client.post("/v1/internal/gateway/settle", json=settle(auth))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["cost_microdollars"] == expected
    replay = client.post("/v1/internal/gateway/settle", json=settle(auth))
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["generation_id"] == result.json()["data"]["generation_id"]


@pytest.mark.parametrize("changes", [
    {"provider": {"only": ["tinfoil"]}},
    {"model": "openai/whisper-1", "provider": {"min_privacy": "confidential"}},
    {"model": "mistralai/voxtral-mini-2602", "provider": {"min_privacy": "confidential"}},
    {"estimated_input_tokens": 10}, {"transcription_duration_ms": 0},
    {"transcription_duration_ms": 600_001}, {"request_fingerprint": ""},
    {"transcription_duration_ms": True}, {"transcription_duration_ms": "1250"},
    {"additional_cost_reservation_microdollars": 1}, {"route_type": "chat.completions"},
])
def test_audio_preflight_rejects_before_reserve(client: TestClient, transcription_key: str, changes: dict[str, Any]) -> None:
    result = authorize(client, transcription_key, **changes)
    assert result.status_code in (400, 422), result.text


@pytest.mark.parametrize("changes", [
    {"actual_input_tokens": 1}, {"actual_output_tokens": 1},
    {"additional_cost_microdollars": 0}, {"additional_cost_microdollars": 100_000},
    {"route_type": "chat.completions"},
])
def test_audio_settlement_rejects_drift(client: TestClient, transcription_key: str, changes: dict[str, Any]) -> None:
    response = authorize(client, transcription_key)
    assert response.status_code == 200, response.text
    result = client.post("/v1/internal/gateway/settle", json=settle(response.json()["data"], **changes))
    assert result.status_code == 400, result.text


def test_audio_catalog_has_explicit_units(client: TestClient) -> None:
    response = client.get("/v1/models?output_modalities=transcription")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    assert set(rows) == set(TRANSCRIPTION_MODELS)
    for model, row in rows.items():
        assert row["pricing"] == TRANSCRIPTION_MODELS[model].pricing
        assert "prompt" not in row["pricing"]
        assert row["architecture"]["modality"] == "audio->transcription"


def test_audio_confidential_route_and_refund(client: TestClient, transcription_key: str) -> None:
    response = authorize(client, transcription_key, model="openai/whisper-large-v3-turbo", provider={"min_privacy": "confidential", "only": ["tinfoil"]})
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["provider"] == "tinfoil"
    result = client.post("/v1/internal/gateway/refund", json={"authorization_id": auth["authorization_id"], "status_code": 502, "error": "transcription_provider_error", "elapsed_seconds": 1})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("model", TRANSCRIPTION_MODELS)
def test_public_audio_pages_use_audio_units(client: TestClient, model: str) -> None:
    page = client.get("/models/" + model)
    assert page.status_code == 200
    assert "7 MiB" in page.text
    assert TRANSCRIPTION_MODELS[model].price_label in page.text
