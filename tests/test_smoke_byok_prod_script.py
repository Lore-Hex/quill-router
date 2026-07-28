from __future__ import annotations

import httpx
import pytest

from scripts.smoke_byok_prod import (
    SmokeConfig,
    SmokeFailure,
    _assert_byok_route,
    _assert_secret_absent,
    _chat_body,
    _require_status,
    _wait_for_generation,
)


@pytest.fixture
def config() -> SmokeConfig:
    return SmokeConfig(
        control_base="https://trustedrouter.com/v1",
        api_base="https://api.trustedrouter.com/v1",
        provider="cerebras",
        model="cerebras/gpt-oss-120b",
        provider_key="csk-provider-secret",
        timeout_seconds=120,
    )


def test_chat_body_forces_one_byok_provider_without_fallback(
    config: SmokeConfig,
) -> None:
    body = _chat_body(config, "marker")

    assert body["model"] == "cerebras/gpt-oss-120b"
    assert body["provider"] == {
        "only": ["cerebras"],
        "usage": "byok",
        "allow_fallbacks": False,
    }
    assert body["max_tokens"] == 32


def test_response_guard_rejects_raw_provider_key() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.test"),
        json={"data": {"api_key": "csk-provider-secret"}},
    )

    with pytest.raises(SmokeFailure, match="leaked"):
        _require_status(
            response,
            {200},
            context="management response",
            raw_secret="csk-provider-secret",  # noqa: S106 - synthetic leak sentinel.
        )


def test_secret_guard_handles_nested_values() -> None:
    with pytest.raises(SmokeFailure, match="leaked"):
        _assert_secret_absent(
            {"data": [{"nested": "prefix-csk-provider-secret-suffix"}]},
            "csk-provider-secret",
            context="nested payload",
        )


def test_route_assertion_requires_forced_provider_and_byok_usage(
    config: SmokeConfig,
) -> None:
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "BYOK_OK"}}],
        "trustedrouter": {
            "selected_provider": "cerebras",
            "usage_type": "BYOK",
            "generation_id": "gen_123",
        },
    }

    assert _assert_byok_route(payload, config) == "gen_123"

    payload["trustedrouter"]["usage_type"] = "Credits"
    with pytest.raises(SmokeFailure, match="expected BYOK"):
        _assert_byok_route(payload, config)


def test_route_assertion_accepts_usage_provider_metadata_fallback(
    config: SmokeConfig,
) -> None:
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "BYOK_OK"}}],
        "trustedrouter": {},
        "usage": {
            "provider_usage": {
                "selected_provider": "cerebras",
                "usage_type": "BYOK",
                "generation_id": "gen_fallback",
            }
        },
    }

    assert _assert_byok_route(payload, config) == "gen_fallback"


def test_generation_lookup_tolerates_transient_replication_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(404, request=request, json={"error": {"type": "not_found"}})
        return httpx.Response(
            200,
            request=request,
            json={"data": {"id": "gen_123", "usage_type": "BYOK"}},
        )

    monkeypatch.setattr("scripts.smoke_byok_prod.time.sleep", lambda _seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = _wait_for_generation(
            client,
            control_base="https://trustedrouter.test/v1",
            inference_key="sk-tr-v1-test",
            generation_id="gen_123",
            raw_secret="csk-provider-secret",  # noqa: S106 - synthetic leak sentinel.
        )

    assert result["data"]["id"] == "gen_123"
    assert calls == 2
