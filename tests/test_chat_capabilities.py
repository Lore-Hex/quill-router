"""Reasoning controls must describe native switches, not reasoning output."""

import pytest
from fastapi.testclient import TestClient

from trusted_router.chat_capabilities import reasoning_modes


@pytest.mark.parametrize(
    ("provider", "model", "supported"),
    [
        ("zai", "z-ai/glm-5.2", True),
        ("zai", "z-ai/glm-5.3", False),
        ("zai", "z-ai/glm-5.3-flash", False),
        ("novita", "z-ai/glm-5.2", False),
        ("deepseek", "deepseek/deepseek-v4-flash-0731", True),
        ("deepseek", "deepseek/deepseek-r1", False),
        ("kimi", "moonshotai/kimi-k2.6", True),
        ("azure", "moonshotai/kimi-k2.6", False),
        ("openai", "openai/gpt-5.5", False),
        ("unknown", "unknown/model", False),
    ],
)
def test_only_verified_hybrid_routes_advertise_reasoning_controls(
    provider: str, model: str, supported: bool,
) -> None:
    assert reasoning_modes(provider, model) == (["off", "on"] if supported else [])


def test_endpoint_reasoning_controls_are_provider_specific(client: TestClient) -> None:
    response = client.get("/v1/models/z-ai/glm-5.2/endpoints")
    assert response.status_code == 200
    endpoints = response.json()["data"]
    assert endpoints
    direct = [endpoint for endpoint in endpoints if endpoint["provider"] == "zai"]
    assert direct
    for endpoint in endpoints:
        assert endpoint["trustedrouter"]["reasoning_modes"] == (
            ["off", "on"] if endpoint["provider"] == "zai" else []
        )
