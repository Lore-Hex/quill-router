from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.ai_iq import ai_iq_candidates, ai_iq_catalog_payload, ai_iq_for_model


def test_ai_iq_candidate_mapping_handles_provider_qualified_model_ids() -> None:
    assert "opus-4.8" in ai_iq_candidates("anthropic/claude-opus-4.8")
    assert "gemini-3.1-pro" in ai_iq_candidates("google/gemini-3.1-pro-preview")
    assert "gemma-4-31b" in ai_iq_candidates("google/gemma-4-31b-it")


def test_ai_iq_for_model_returns_public_profile_metadata() -> None:
    opus = ai_iq_for_model("anthropic/claude-opus-4.8", test_mode=True)
    assert opus is not None
    assert opus["id"] == "opus-4.8"
    assert opus["iq"] == 128
    assert opus["rank"] == 3
    assert opus["url"] == "https://aiiq.org/models/opus-4.8/"

    gemini = ai_iq_for_model("google/gemini-3.1-pro-preview", test_mode=True)
    assert gemini is not None
    assert gemini["id"] == "gemini-3.1-pro"


def test_ai_iq_catalog_payload_is_keyed_by_trustedrouter_model_id() -> None:
    payload = ai_iq_catalog_payload(
        [
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.7-code",
            "unknown/provider-model",
        ],
        test_mode=True,
    )

    assert payload["source"] == "AI IQ"
    assert payload["source_url"] == "https://aiiq.org/api/"
    assert payload["models"]["minimax/minimax-m3"]["iq"] == 109
    assert payload["models"]["moonshotai/kimi-k2.7-code"]["rank"] == 16
    assert "unknown/provider-model" not in payload["models"]


def test_public_ai_iq_endpoint_is_normalized_for_choose_app(client: TestClient) -> None:
    response = client.get("/ai-iq/models.json")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public")
    payload = response.json()
    assert payload["source"] == "AI IQ"
    assert payload["models"]["minimax/minimax-m3"]["url"] == (
        "https://aiiq.org/models/minimax-m3/"
    )
    assert payload["models"]["anthropic/claude-opus-4.8"]["iq"] == 128
