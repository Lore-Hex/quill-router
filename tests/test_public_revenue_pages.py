from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_revenue_pages_are_public(client: TestClient) -> None:
    markers = {
        "/compare/openrouter": "Routing convenience with a prompt path you can inspect.",
        "/compare/vercel-ai-gateway": "Use Vercel where it fits.",
        "/compare/litellm": "LiteLLM for your own infra",
        "/docs/migrate-from-openrouter": "Change base_url",
        "/security": "TrustedRouter does not store prompt or output content by default.",
    }

    for path, marker in markers.items():
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text
        assert "Approved short copy only" not in response.text
        assert "OpenAI compatible API" in response.text
        assert "Invalid API key" not in response.text
        assert "Continue with MetaMask" in response.text


def test_revenue_pages_support_link_checkers(client: TestClient) -> None:
    paths = [
        "/compare/openrouter",
        "/compare/vercel-ai-gateway",
        "/compare/litellm",
        "/docs/migrate-from-openrouter",
        "/security",
        "/models",
    ]

    for path in paths:
        assert client.head(path).status_code == 200
        slash_response = client.get(f"{path}/", follow_redirects=False)
        assert slash_response.status_code == 200


def test_public_models_page_does_not_require_api_key(client: TestClient) -> None:
    response = client.get("/models")

    assert response.status_code == 200
    assert "Public catalog" in response.text
    assert "trustedrouter/auto" in response.text
    assert "API JSON remains" in response.text
    assert '<span class="pill" title="kimi">Kimi</span>' in response.text
    assert '<span class="pill" title="parasail">Parasail</span>' in response.text
    assert '<span class="pill" title="tinfoil">Tinfoil</span>' in response.text


def test_public_model_detail_lists_distinct_serving_providers(client: TestClient) -> None:
    response = client.get("/models/moonshotai/kimi-k2.6")

    assert response.status_code == 200
    assert "Providers serving this model" in response.text
    assert "Endpoints</th>" in response.text
    for provider in ["kimi", "parasail", "phala", "together", "tinfoil", "novita"]:
        assert f'title="{provider}"' in response.text


def test_dashboard_links_to_public_models_not_keyed_api_catalog(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/models"' in response.text
    assert 'href="https://api.quillrouter.com/v1/models"' not in response.text
    assert "Migration credits" in response.text
    assert "spending more than $100 per month on LLMs" in response.text
    assert "Provider failover" in response.text
    assert "Public status separates router health from provider health" in response.text
    assert "/static/hero-router-scene.js" in response.text
    assert "data-router-scene" in response.text
    assert "Change <span class=\"mono\">base_url</span>" in response.text


def test_console_credit_note_is_manual(client: TestClient) -> None:
    user = STORE.ensure_user("alice@example.com")
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)

    response = client.get("/console/credits")

    assert response.status_code == 200
    assert "This console does not grant them automatically" in response.text


def test_revenue_loop_docs_define_google_sheet_and_interview_rules() -> None:
    root = Path(__file__).resolve().parents[1]
    loop_doc = (root / "docs/revenue-loop.md").read_text()
    interview_doc = (root / "docs/founder-interview.md").read_text()
    sheet_csv = (root / "docs/revenue-loop-google-sheet.csv").read_text()

    assert "Google Sheets is the CRM source of truth" in loop_doc
    assert "Do not send outreach without human approval" in loop_doc
    assert "Do not paraphrase claims" in interview_doc
    assert "approved_message" in sheet_csv
    assert "opt_out" in sheet_csv
