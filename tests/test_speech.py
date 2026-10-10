from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.speech import SPEECH_MODELS
from trusted_router.storage import STORE


@pytest.fixture
def speech_key(client: TestClient, user_headers: dict[str, str]) -> str:
    return str(
        client.post("/v1/keys", headers=user_headers, json={"name": "speech"}).json()["data"][
            "hash"
        ]
    )


def authorize(client: TestClient, key: str, **changes: Any) -> Any:
    return client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key,
            "model": "x-ai/grok-voice-tts-1.0",
            "route_type": "audio.speech",
            "estimated_input_tokens": 0,
            "max_output_tokens": 1,
            "speech_input_characters": 100,
            "idempotency_key": "speech-one",
            "request_fingerprint": "a" * 64,
            **changes,
        },
    )


def settlement(auth: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "authorization_id": auth["authorization_id"],
        "request_id": "speech-result",
        "actual_input_tokens": 0,
        "actual_output_tokens": 0,
        "elapsed_seconds": 1,
        "route_type": "audio.speech",
        "finish_reason": "stop",
        "additional_cost_microdollars": auth["additional_cost_reservation_microdollars"],
        **changes,
    }


def test_speech_catalog(client: TestClient) -> None:
    response = client.get("/v1/models?output_modalities=speech")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    assert set(rows) == set(SPEECH_MODELS)
    for mid, spec in SPEECH_MODELS.items():
        assert not MODELS[mid].supports_chat
        assert rows[mid]["architecture"]["modality"] == "text->speech"
        assert rows[mid]["trustedrouter"]["speech"]["billing_unit"] == (
            "tokens" if spec.token_billed else "input_characters"
        )
        assert ("prompt" in rows[mid]["pricing"]) == spec.token_billed
        assert rows[mid]["supported_voices"] == list(spec.voices)
        assert endpoints_for_model(mid)
        assert all(not e.is_byok for e in endpoints_for_model(mid))
        from trusted_router.routes.catalog import _endpoint_pricing_payload

        for endpoint in endpoints_for_model(mid):
            assert _endpoint_pricing_payload(endpoint) == rows[mid]["pricing"]


@pytest.mark.parametrize(
    "model", [mid for mid, spec in SPEECH_MODELS.items() if not spec.token_billed]
)
def test_speech_bills_exact_frozen_quote_once(
    client: TestClient, speech_key: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = authorize(client, speech_key, model=model)
    assert result.status_code == 200, result.text
    auth = result.json()["data"]
    quote = SPEECH_MODELS[model].quote(100)
    assert auth["estimated_cost_microdollars"] == quote
    assert auth["additional_cost_reservation_microdollars"] == quote
    monkeypatch.setitem(
        SPEECH_MODELS,
        model,
        replace(SPEECH_MODELS[model], cost_microdollars_per_million_characters=99_000_000),
    )
    replay = authorize(client, speech_key, model=model)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["additional_cost_reservation_microdollars"] == quote
    assert replay.json()["data"]["idempotent_replay"]
    body = settlement(auth)
    for _ in range(2):
        settled = client.post("/v1/internal/gateway/settle", json=body)
        assert settled.status_code == 200, settled.text
        data = settled.json()["data"]
        assert data["cost_microdollars"] == quote
        assert data["input_tokens"] == data["output_tokens"] == 0
    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None and generation.route_type == "audio.speech"


@pytest.mark.parametrize(
    "changes",
    [
        {"speech_input_characters": None},
        {"speech_input_characters": 0},
        {"speech_input_characters": 60_001},
        {"estimated_input_tokens": 10},
        {"model": "mistralai/voxtral-mini-tts-2603", "speech_input_characters": 10_001},
        {"models": ["x-ai/grok-voice-tts-1.0"]},
        {"service_tier": "priority"},
        {"provider": {"usage": "byok"}},
        {"provider": {"only": ["openai"]}},
        {"provider": {"ignore": ["grok"]}},
        {"provider": {"min_privacy": "confidential"}},
        {"route_type": "chat.completions"},
        {"request_fingerprint": ""},
        {"provider": {"max_price": {"prompt": 1}}},
    ],
)
def test_speech_rejects_invalid_admission(
    client: TestClient, speech_key: str, changes: dict[str, Any]
) -> None:
    result = authorize(client, speech_key, **changes)
    assert result.status_code in (400, 422), result.text


@pytest.mark.parametrize(
    "changes",
    [
        {"additional_cost_microdollars": 0},
        {"additional_cost_microdollars": 999999},
        {"route_type": "images"},
        {"route_type": None},
        {"actual_input_tokens": 1},
        {"actual_output_tokens": 1},
    ],
)
def test_speech_rejects_underbilling_and_route_confusion(
    client: TestClient, speech_key: str, changes: dict[str, Any]
) -> None:
    result = authorize(client, speech_key)
    assert result.status_code == 200, result.text
    result = client.post(
        "/v1/internal/gateway/settle", json=settlement(result.json()["data"], **changes)
    )
    assert result.status_code == 400, result.text


def test_speech_refund_accepts_generic_abort(client: TestClient, speech_key: str) -> None:
    result = authorize(client, speech_key)
    assert result.status_code == 200, result.text
    result = client.post(
        "/v1/internal/gateway/refund",
        json=settlement(
            result.json()["data"],
            route_type=None,
            additional_cost_microdollars=0,
            error_status=502,
            error_type="speech_provider_error",
        ),
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["cost_microdollars"] == 0


def test_speech_quote_rounds_up_and_rejects_boolean() -> None:
    spec = SPEECH_MODELS["x-ai/grok-voice-tts-1.0"]
    assert spec.quote(1) == 16
    assert spec.quote(100) == 1583
    for invalid in (True, -1, 0, 60001):
        with pytest.raises(ValueError):
            spec.quote(invalid)


def test_elevenlabs_and_microsoft_price_parsers() -> None:
    from scripts.pricing.speech import parse_elevenlabs_prices, parse_microsoft_prices

    html = "".join(
        f"<section><h3>{name}</h3><p>Text to Speech</p><p>${price}</p><p>Price per 1K characters</p></section>"
        for name, price in (("v3", "0.08"), ("v2 Multilingual", "0.08"), ("Flash / Turbo", "0.04"))
    )
    rates = parse_elevenlabs_prices(html)
    assert rates["elevenlabs/eleven-v3"] == 80_000_000
    assert rates["elevenlabs/eleven-flash-v2.5"] == 40_000_000
    for broken in (
        html.replace("1K characters", "1M tokens"),
        html.replace("$0.08", "$0.08 $0.02"),
        html.replace("<h3>v3</h3>", "<h3>v4</h3>"),
    ):
        with pytest.raises(ValueError):
            parse_elevenlabs_prices(broken)
    article = "MAI-Voice-2.1 is available at $22 per 1M characters; MAI-Voice-2.1 Flash is available at $15 per 1M characters"
    assert parse_microsoft_prices(article) == {
        "microsoft/mai-voice-2.1": 22_000_000,
        "microsoft/mai-voice-2.1-flash": 15_000_000,
    }
    with pytest.raises(ValueError):
        parse_microsoft_prices(article.replace("characters", "tokens"))


def test_gemini_discovery_preserves_speech_admission(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from scripts.pricing.base import ProviderPricingResult
    from scripts.pricing.providers import gemini

    model = "google/gemini-3.8-flash-tts"
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": model,
                        "model_type": "chat",
                        "routable": False,
                        "routable_reason": "awaiting-price",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(gemini, "MANIFEST_PATH", path)
    monkeypatch.setattr(
        gemini,
        "_DISCOVERED_MANIFEST_ROWS",
        {model: {"id": model, "upstream_id": "gemini-3.8-flash-tts"}},
    )
    result = ProviderPricingResult(slug="gemini", prices={}, source="api")
    gemini.write_provider_manifest(result)
    row = json.loads(path.read_text())["models"][0]
    assert row["routable"] is True and row["model_type"] == "speech"
    assert row["endpoints"] == ["audio/speech"]
    row.update(routable=False, routable_reason="operator-hold")
    path.write_text(json.dumps({"models": [row]}))
    gemini.write_provider_manifest(result)
    assert json.loads(path.read_text())["models"][0]["routable"] is False


def test_speech_enforces_key_limit(client: TestClient, user_headers: dict[str, str]) -> None:
    key = client.post(
        "/v1/keys", headers=user_headers, json={"name": "tiny", "limit": 0.000001}
    ).json()["data"]["hash"]
    response = authorize(client, key)
    assert response.status_code == 402, response.text
    assert response.json()["error"]["type"] == "key_limit_exceeded"


def test_speech_enforces_balance(
    client: TestClient, speech_key: str, user_headers: dict[str, str]
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    STORE.credit_money[workspace_id].total_credits_microdollars = 0
    response = authorize(client, speech_key)
    assert response.status_code == 402, response.text
    assert response.json()["error"]["type"] == "insufficient_credits"


def test_speech_price_parser_is_unit_strict() -> None:
    from scripts.pricing.speech import parse_character_price

    xai = "<table><tr><td>Pricing</td><td>$15.00 / 1M chars</td></tr></table>"
    mistral = "<table><tr><td>Voxtral TTS</td><td>$0 /M Chars</td><td>$0 /M Chars</td><td>$16 /M Chars</td></tr></table>"
    assert parse_character_price("x-ai/grok-voice-tts-1.0", xai) == 15_000_000
    assert parse_character_price("mistralai/voxtral-mini-tts-2603", mistral) == 16_000_000
    for html in ("", xai.replace("chars", "tokens"), xai + xai.replace("15.00", "30.00")):
        with pytest.raises(ValueError):
            parse_character_price("x-ai/grok-voice-tts-1.0", html)


def test_speech_doc_contract(client: TestClient) -> None:
    operation = client.app.openapi()["paths"]["/audio/speech"]["post"]  # type: ignore[attr-defined]
    assert operation["servers"][0]["url"] == "https://api.trustedrouter.com/v1"
    assert "audio/mpeg" in operation["responses"]["200"]["content"]
    assert "application/json" not in operation["responses"]["200"]["content"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["required"] == [
        "model",
        "input",
        "voice",
    ]


def test_speech_public_prices_are_character_based(client: TestClient) -> None:
    from trusted_router.dashboard import _model_detail_view, _model_view

    for mid in [mid for mid, spec in SPEECH_MODELS.items() if not spec.token_billed]:
        row = _model_view(MODELS[mid], test_mode=True)
        assert str(row["prompt_price"]).endswith("/M characters")
        assert row["completion_price"] == "Included"
        detail = _model_detail_view(MODELS[mid], test_mode=True)
        assert detail["speech"]
        for endpoint in detail["endpoints"]:
            assert endpoint["prompt_price"] == row["prompt_price"]
        page = client.get(f"/models/{mid}")
        assert page.status_code == 200
        assert "/M characters" in page.text


def test_speech_price_refresh_preserves_snapshot_on_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from scripts.pricing import speech

    snapshot = tmp_path / "speech.json"
    before = json.dumps({"prices": {key: 16_000_000 for key in speech.SOURCES}})
    snapshot.write_text(before)
    monkeypatch.setattr(speech, "SNAPSHOT", snapshot)

    class Response:
        text = "<table><tr><td>Pricing</td><td>$16.00 / 1M chars</td></tr></table>"

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(speech.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="Missing"):
        speech.refresh()
    assert snapshot.read_text() == before


def test_speech_idempotency_binds_character_count_and_content(
    client: TestClient, speech_key: str
) -> None:
    assert authorize(client, speech_key).status_code == 200
    for changes in ({"speech_input_characters": 101}, {"request_fingerprint": "b" * 64}):
        result = authorize(client, speech_key, **changes)
        assert result.status_code == 409, result.text


@pytest.mark.parametrize("model", [mid for mid, spec in SPEECH_MODELS.items() if spec.token_billed])
def test_gemini_speech_bills_reported_tokens_not_reserved_maximum(
    client: TestClient, speech_key: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from trusted_router import catalog

    result = authorize(
        client, speech_key, model=model, estimated_input_tokens=8192, max_output_tokens=16384
    )
    assert result.status_code == 200, result.text
    auth = result.json()["data"]
    assert auth["additional_cost_reservation_microdollars"] == 0
    assert auth["estimated_cost_microdollars"] > 100_000
    stored = STORE.get_gateway_authorization(auth["authorization_id"])
    assert stored is not None and stored.pricing_snapshot is not None
    for changes in (
        {"actual_output_tokens": 0},
        {"actual_output_tokens": 16385},
        {"actual_input_tokens": 8193},
        {"additional_cost_microdollars": 1},
        {"route_type": "chat.completions"},
        {"service_tier": "priority"},
    ):
        body = settlement(auth, actual_input_tokens=6, actual_output_tokens=52)
        body.update(changes)
        rejected = client.post("/v1/internal/gateway/settle", json=body)
        assert rejected.status_code == 400, rejected.text
    body = settlement(auth, actual_input_tokens=6, actual_output_tokens=52)
    endpoint = endpoints_for_model(model)[0]
    expected = (
        6 * endpoint.prompt_price_microdollars_per_million_tokens
        + 52 * endpoint.completion_price_microdollars_per_million_tokens
        + 500_000
    ) // 1_000_000
    monkeypatch.setitem(
        catalog.MODEL_ENDPOINTS,
        endpoint.id,
        replace(
            endpoint,
            prompt_price_microdollars_per_million_tokens=50_000_000,
            completion_price_microdollars_per_million_tokens=100_000_000,
        ),
    )
    for _ in range(2):
        response = client.post("/v1/internal/gateway/settle", json=body)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["cost_microdollars"] == expected
        assert data["input_tokens"] == 6 and data["output_tokens"] == 52
    page = client.get(f"/models/{model}")
    assert "/M audio tokens" in page.text and "/M characters" not in page.text


def test_gemini_speech_admission_rejects_under_reserved_tokens(
    client: TestClient, speech_key: str
) -> None:
    model = "google/gemini-3.8-flash-tts"
    for changes in (
        {},
        {"estimated_input_tokens": 8192, "max_output_tokens": 1},
        {"estimated_input_tokens": 1, "max_output_tokens": 16384},
        {
            "estimated_input_tokens": 8192,
            "max_output_tokens": 16384,
            "provider": {"min_privacy": "confidential"},
        },
    ):
        assert authorize(client, speech_key, model=model, **changes).status_code == 400


def test_gemini_speech_price_schedule() -> None:
    from datetime import date

    from scripts.pricing.speech import parse_gemini_token_prices

    html = "".join(
        f"""<h2>{name}</h2><h3>Standard</h3><table>
    <tr><th></th><th>Free</th><th>Paid Tier, per 1M tokens in USD</th></tr>
    <tr><td>Input price</td><td>Free</td><td>$0.50 (text) through December 31, 2026. $1.00 (text) starting January 1, 2027.</td></tr>
    <tr><td>Output price</td><td>Free</td><td>${rate} (audio) through December 31, 2026. ${rate * 2} (audio) starting January 1, 2027.</td></tr>
    </table><h3>Batch</h3><table><tr><td>Output price</td><td>Free</td><td>$1 (audio)</td></tr></table>"""
        for name, rate in (("Gemini 3.8 Flash TTS", 9), ("Gemini 3.8 Flash-Lite TTS", 6))
    )
    for year, scale in ((2026, 1), (2027, 2)):
        rates = parse_gemini_token_prices(html, today=date(year, 12, 31))
        assert rates["google/gemini-3.8-flash-tts"] == {
            "input": 500_000 * scale,
            "output": 9_000_000 * scale,
        }
    with pytest.raises(ValueError):
        parse_gemini_token_prices(html.replace("per 1M tokens", "per hour"))
