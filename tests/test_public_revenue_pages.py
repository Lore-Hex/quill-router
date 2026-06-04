from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_revenue_pages_are_public(client: TestClient) -> None:
    markers = {
        "/compare/openrouter": "OpenRouter, but you can verify the prompt path.",
        "/compare/vercel-ai-gateway": "Use Vercel where it fits.",
        "/compare/litellm": "LiteLLM for your own infra",
        "/docs/migrate-from-openrouter": "Change base_url",
        "/security": "TrustedRouter does not store prompt or output content by default.",
        # SEO landing pages — each targets a high-intent buyer query.
        # The marker is a load-bearing headline from the page so a
        # silent template breakage gets caught here.
        "/openrouter-alternative": "An OpenRouter alternative built around verifiable privacy.",
        "/private-llm-api": "A private LLM API where \"private\" means cryptographically verified.",
        "/hipaa-llm-api": "The LLM API whose privacy posture is verifiable",
        "/llm-zero-data-retention": "Zero data retention as a verifiable property",
        "/claude-api-privacy": "Call Claude through a prompt path you can verify.",
        # Round-2 competitor-alternative + category pages.
        "/litellm-alternative": "LiteLLM lets you self-host.",
        "/portkey-alternative": "Portkey logs every request.",
        "/confidential-computing-llm": "Run LLM inference behind hardware attestation",
        "/tinfoil-alternative": "Same verifiable-privacy bet.",
    }

    for path, marker in markers.items():
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert marker in response.text, f"{path} missing marker {marker!r}"
        assert "Approved short copy only" not in response.text
        assert "OpenAI compatible API" in response.text
        assert "Invalid API key" not in response.text
        assert "Continue with MetaMask" in response.text
        # Every public page must unfurl: og:title + a card image.
        assert 'property="og:title"' in response.text, f"{path} missing og:title"
        assert 'property="og:image"' in response.text, f"{path} missing og:image"
        assert 'name="twitter:card"' in response.text, f"{path} missing twitter:card"


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


def test_public_model_detail_uses_service_structured_data(client: TestClient) -> None:
    response = client.get("/models/moonshotai/kimi-k2.6")

    assert response.status_code == 200
    match = re.search(
        r'<script type="application/ld\+json">(?P<payload>.*?)</script>',
        response.text,
    )
    assert match is not None
    payload = json.loads(match.group("payload"))
    assert payload["@type"] == "Service"
    assert payload["offers"]["@type"] == "Offer"
    assert payload["serviceType"] == "AI model routing API"
    assert "aggregateRating" not in payload
    assert "review" not in payload
    assert "hasMerchantReturnPolicy" not in payload["offers"]
    assert "shippingDetails" not in payload["offers"]


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
    # Hero leads with the "tell your agent to move you over" prompt flow.
    assert "Move over in one prompt" in response.text
    assert "Migrate this project to TrustedRouter" in response.text
    assert "data_collection" in response.text  # privacy-level routing pref
    assert "Sign up &amp; get a key" in response.text


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
