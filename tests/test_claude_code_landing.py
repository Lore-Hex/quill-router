from __future__ import annotations

import re

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_claude_code_landing_is_a_single_action_funnel(client: TestClient) -> None:
    response = client.get("/vibe-coders")

    assert response.status_code == 200
    assert "Cut your AI bill in 10 seconds." in response.text
    assert response.text.count("Get my one-paste message") == 3
    assert "The 10-second flow" in response.text
    assert "Create your key" in response.text
    assert "Copy one short message" in response.text
    assert "Paste into a new agent chat" in response.text
    assert "Just paste one short message." in response.text
    assert "your complete key is inserted" in response.text
    assert "Same agent. Same settings. Extra models." in response.text
    assert "stream=true" not in response.text
    assert "trustedrouter/cheap" in response.text
    assert "Every model. In seconds." in response.text
    assert "How should I think about savings?" in response.text
    assert 'rel="canonical" href="https://trustedrouter.com/vibe-coders"' in response.text
    assert 'property="og:image" content="https://trustedrouter.com/static/og/claude-code.png"' in response.text
    assert "YOUR_TRUSTEDROUTER_API_KEY" not in response.text
    assert "sk-tr-v1-..." not in response.text
    assert re.search(
        r"\b(?:no|not|never|without|nothing|don't|doesn't|cannot|can't)\b",
        response.text,
        re.IGNORECASE,
    ) is None


def test_vibe_coders_landing_is_indexed_and_claude_alias_redirects(
    client: TestClient,
) -> None:
    sitemap = client.get("/sitemap-core.xml")
    alias = client.get("/claude-code", follow_redirects=False)

    assert sitemap.status_code == 200
    assert "<loc>https://trustedrouter.com/vibe-coders</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/claude-code</loc>" not in sitemap.text
    assert alias.status_code == 301
    assert alias.headers["location"] == "/vibe-coders"


def test_api_key_reveal_builds_a_streaming_agent_chat_message_without_reconfiguration(
    client: TestClient,
) -> None:
    user = STORE.ensure_user("claude-code-landing@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=user.email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    response = client.post(
        "/console/api-keys",
        data={"name": "Claude Code", "limit": ""},
    )

    assert response.status_code == 200
    assert (
        "Paste this short message into a Claude Code, Codex, or your favorite agent chat."
        in response.text
    )
    assert "Use TrustedRouter with the key below to ask DeepSeek" in response.text
    assert "stream the answer into this chat as it arrives" in response.text
    assert "Keep this agent's model" not in response.text
    assert "Use it in memory for this request" not in response.text
    assert "stream=true" not in response.text
    assert "ANTHROPIC_BASE_URL" not in response.text
    assert ".claude/settings.local.json" not in response.text
    assert "restart Claude Code" not in response.text
