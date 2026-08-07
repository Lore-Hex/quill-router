from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_claude_code_landing_is_a_single_action_funnel(client: TestClient) -> None:
    response = client.get("/claude-code")

    assert response.status_code == 200
    assert "Cut your Claude Code token bill in 10 seconds." in response.text
    assert response.text.count("Create my paste-ready key") == 3
    assert "Claude Code does the setup." in response.text
    assert "No placeholders. No scavenger hunt." in response.text
    assert "your complete key is inserted" in response.text
    assert "trustedrouter/cheap" in response.text
    assert "Every model. In seconds." in response.text
    assert "No prompt or output logs, always." in response.text
    assert "Will every task cost less?" in response.text
    assert 'rel="canonical" href="https://trustedrouter.com/claude-code"' in response.text
    assert 'property="og:image" content="https://trustedrouter.com/static/og/claude-code.png"' in response.text
    assert "YOUR_TRUSTEDROUTER_API_KEY" not in response.text
    assert "sk-tr-v1-..." not in response.text


def test_claude_code_landing_is_indexed_and_vibe_alias_is_canonical(
    client: TestClient,
) -> None:
    sitemap = client.get("/sitemap-core.xml")
    alias = client.get("/vibe-coders", follow_redirects=False)

    assert sitemap.status_code == 200
    assert "<loc>https://trustedrouter.com/claude-code</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/vibe-coders</loc>" not in sitemap.text
    assert alias.status_code == 301
    assert alias.headers["location"] == "/claude-code"


def test_api_key_reveal_builds_a_complete_claude_code_setup_message(
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
    assert "ANTHROPIC_BASE_URL=https://api.trustedrouter.com" in response.text
    assert "ANTHROPIC_MODEL=trustedrouter/cheap" in response.text
    assert ".claude/settings.local.json" in response.text
    assert "preserve every other setting" in response.text
    assert "Verify the file is ignored by git" in response.text
