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
        "/docs/synth": "Run a panel of models inside the attested gateway.",
        "/docs/fusion": "Run a panel of models inside the attested gateway.",
        "/synth": "Synthesize many models into one perfect frontier answer.",
        "/fusion": "Synthesize many models into one perfect frontier answer.",
        "/blog": "TrustedRouter blog",
        "/blog/fusion-evals-open-source": "New SOTA: TrustedRouter Synth beats Fable and Frontier",
        "/security": "TrustedRouter does not store prompt or output content by default.",
        "/eu": "Use the EU gateway and an EU-focused model alias.",
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
        "/openai-compatible-llm-api": "Keep the SDK. Change the base URL.",
        "/kimi-k2-api": "Kimi K2 with provider fallback and measured routes.",
        "/gemini-flash-alternative": "Compare Gemini Flash with the cheapest good routes.",
        "/llm-provider-latency-benchmarks": "Provider speed data from real routed requests.",
    }

    for path, marker in markers.items():
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert marker in response.text, f"{path} missing marker {marker!r}"
        assert "Approved short copy only" not in response.text
        # Blog pages intentionally drop the marketing hero (and its
        # "OpenAI compatible API" eyebrow); every other public page keeps it.
        if not path.startswith("/blog"):
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
        "/docs/synth",
        "/docs/fusion",
        "/synth",
        "/fusion",
        "/blog",
        "/blog/fusion-evals-open-source",
        "/security",
        "/eu",
        "/models",
    ]

    for path in paths:
        assert client.head(path).status_code == 200
        slash_response = client.get(f"{path}/", follow_redirects=False)
        assert slash_response.status_code == 200


def test_agent_discovery_surfaces_model_advisor_skill(client: TestClient) -> None:
    for path in ["/", "/docs", "/docs/agent-setup"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Agent skill" in response.text or "model advisor playbook" in response.text
        assert "codex-skill" in response.text

    for path in ["/docs", "/docs/agent-setup"]:
        response = client.get(path)
        assert "https://github.com/Lore-Hex/LLM-advisor" in response.text
        assert "https://raw.githubusercontent.com/Lore-Hex/LLM-advisor/main/SKILL.md" in response.text

    for path in ["/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Agent model-advisor skill/playbook" in response.text
        assert "trustedrouter-model-advisor" in response.text
        assert "https://github.com/Lore-Hex/LLM-advisor" in response.text


def test_model_advisor_skill_covers_privacy_region_filters_and_blog_context() -> None:
    skill_root = Path("skills/trustedrouter-model-advisor")
    skill_text = (skill_root / "SKILL.md").read_text()

    assert "https://github.com/Lore-Hex/LLM-advisor" in skill_text
    assert "https://raw.githubusercontent.com/Lore-Hex/LLM-advisor/main/SKILL.md" in skill_text
    assert not (skill_root / "references/model-selection.md").exists()
    assert not (skill_root / "agents/openai.yaml").exists()


def test_choose_page_embeds_the_triangle_app(client: TestClient) -> None:
    response = client.get("/choose")

    assert response.status_code == 200
    # Hero + the embedded interactive tool.
    assert "Choose the right model for the job." in response.text
    assert "/static/choose-app.html" in response.text  # iframe src
    assert 'id="tr-choose-frame"' in response.text
    # The renamed privacy tier and the router-route payload show up in copy.
    assert "Trusted Execution Environment" in response.text
    assert "Tinfoil first" not in response.text
    assert "providers such as" not in response.text
    assert "trustedrouter/e2e" in response.text
    assert "trustedrouter/synth" in response.text
    # Must unfurl with the tailored triangle social card (the PNG is checked
    # into static/og/, so _og_image_url resolves it rather than the default).
    assert 'property="og:title"' in response.text
    assert "/static/og/choose.png" in response.text
    # Trailing-slash + HEAD variants both resolve.
    assert client.head("/choose").status_code == 200
    assert client.get("/choose/", follow_redirects=False).status_code == 200


def test_choose_app_static_asset_is_served(client: TestClient) -> None:
    response = client.get("/static/choose-app.html")

    assert response.status_code == 200
    assert "iron triangle of LLMs" in response.text
    # Embedded-mode hook that hides the in-app header inside the iframe.
    assert "tr-choose-height" in response.text
    assert "Tinfoil-first TEE providers" not in response.text
    assert "trustedrouter/e2e" in response.text
    assert "E2E pool includes Tinfoil" not in response.text
    assert "provider route choices shown on each model card" in response.text
    assert "PROVIDER_LOOKUP" in response.text
    assert "AI_IQ_LOOKUP" in response.text
    assert "/v1/models" in response.text
    assert "/ai-iq/models.json" in response.text
    # Privacy floor defaults to Open (any provider), not ZDR.
    assert '<option value="0" selected>' in response.text
    assert "priv: 0," in response.text


def test_synth_playground_is_public_and_uses_browser_key_proxy(client: TestClient) -> None:
    response = client.get("/synth")

    assert response.status_code == 200
    assert "trustedrouter/synth" in response.text
    assert "Synthesize many models into one perfect frontier answer." in response.text
    assert "/chat-proxy/v1" in response.text
    assert "/internal/chat/issue-browser-key" in response.text
    assert "/static/fusion.css" in response.text
    assert "/static/fusion.js" in response.text
    assert "/static/og/synth.png" in response.text
    assert "TrustedRouter Synth compares a model panel and returns one answer" in response.text
    assert "synthesize_non_refusals" in response.text
    assert 'data-action="toggle-fusion-detail-layout"' in response.text
    assert "Judge and fallback judge" in response.text
    assert "Synthesizer and fallback synthesizer" in response.text
    assert "moonshotai/kimi-k2.6" in response.text
    assert "z-ai/glm-5.2" in response.text
    assert client.head("/synth").status_code == 200
    assert client.get("/synth/", follow_redirects=False).status_code == 200
    assert client.head("/fusion").status_code == 200
    assert client.get("/fusion/", follow_redirects=False).status_code == 200


def test_synth_docs_publish_current_gateway_shape(client: TestClient) -> None:
    response = client.get("/docs/synth")

    assert response.status_code == 200
    assert "trustedrouter/synth" in response.text
    assert "trustedrouter:synth" in response.text
    assert "analysis_models" in response.text
    assert "judge_models" in response.text
    assert "fallback_judges" in response.text
    assert "final_models" in response.text
    assert "fallback_final_models" in response.text
    assert "synthesize_non_refusals" in response.text
    assert "/static/og/synth.png" in response.text
    assert "judges with Kimi K2.6" in response.text
    assert "synthesizes with GLM 5.2" in response.text
    assert "moonshotai/kimi-k2.7-code" in response.text
    assert "z-ai/glm-5.2" in response.text
    assert "minimax/minimax-m3" in response.text
    assert "google/gemma-4-31b-it" in response.text
    assert "deepseek/deepseek-v4-pro" in response.text
    assert "Final fallback can switch before the first byte" in response.text
    assert "TrustedRouter stores billing and route metadata, not prompt/output content by default." in response.text
    assert "OpenAI compatible API" in response.text
    assert client.get("/docs/fusion").status_code == 200


def test_homepage_and_nav_link_to_choose(client: TestClient) -> None:
    assert 'href="/choose"' in client.get("/").text
    assert 'href="/choose"' in client.get("/models").text  # _base nav


def test_public_models_page_does_not_require_api_key(client: TestClient) -> None:
    response = client.get("/models")

    assert response.status_code == 200
    assert "Public catalog" in response.text
    assert "trustedrouter/auto" in response.text
    assert "trustedrouter/eu" in response.text
    assert "API JSON remains" in response.text
    assert '<span class="pill" title="kimi">Kimi</span>' in response.text
    assert '<span class="pill" title="parasail">Parasail</span>' in response.text
    assert '<span class="pill" title="tinfoil">Tinfoil</span>' in response.text
    assert 'href="https://aiiq.org/models/kimi-k2.6/"' in response.text
    assert "IQ 116" in response.text


def test_public_model_detail_lists_distinct_serving_providers(client: TestClient) -> None:
    response = client.get("/models/moonshotai/kimi-k2.6")

    assert response.status_code == 200
    assert "Providers serving this model" in response.text
    assert "Endpoints</th>" in response.text
    assert 'href="https://aiiq.org/models/kimi-k2.6/"' in response.text
    assert "IQ 116" in response.text
    for provider in ["kimi", "parasail", "phala", "together", "tinfoil", "novita"]:
        assert f'title="{provider}"' in response.text


def test_public_meta_model_detail_renders_orchestration_components(client: TestClient) -> None:
    response = client.get("/models/trustedrouter/socrates-1.1")

    assert response.status_code == 200
    assert "TrustedRouter Socrates 1.1" in response.text
    assert "<span class=\"pill\">advisor</span>" in response.text
    assert "<span class=\"pill\">named preset</span>" in response.text
    assert "Models used by this orchestration" in response.text
    assert "xiaomi/mimo-v2.5-pro-ultraspeed" in response.text
    assert "minimax/minimax-m3" in response.text
    assert "z-ai/glm-5.2-fast" in response.text
    assert "deepseek/deepseek-v4-flash" in response.text
    assert "trustedrouter/zeus-1.0" in response.text
    assert "Model not found" not in response.text
    assert "/models/trustedrouter/socrates-1.1/providers" not in response.text

    rolling = client.get("/models/trustedrouter/socrates")
    assert rolling.status_code == 200
    assert "<span class=\"pill\">advisor</span>" in rolling.text
    assert "<span class=\"pill\">rolling alias</span>" in rolling.text
    assert "Canonical: <a href=\"/models/trustedrouter/socrates-1.1\"" in rolling.text


def test_public_athena_model_detail_hides_orchestration_components(client: TestClient) -> None:
    response = client.get("/models/trustedrouter/athena")

    assert response.status_code == 200
    assert "TrustedRouter Athena" in response.text
    assert "Models used by this orchestration" not in response.text
    assert "z-ai/glm-5.2-fast" not in response.text
    assert "moonshotai/kimi-k2.7-code" not in response.text
    assert "trustedrouter/prometheus-1.0-1m" not in response.text
    assert "Model not found" not in response.text


def test_public_model_detail_uses_service_structured_data(client: TestClient) -> None:
    response = client.get("/models/moonshotai/kimi-k2.6")

    assert response.status_code == 200
    match = re.search(
        r'<script type="application/ld\+json">(?P<payload>.*?)</script>',
        response.text,
    )
    assert match is not None
    payload = json.loads(match.group("payload"))
    graph = {item["@type"]: item for item in payload["@graph"]}
    service = graph["Service"]
    assert service["offers"]["@type"] == "Offer"
    assert service["serviceType"] == "AI model routing API"
    assert graph["BreadcrumbList"]["itemListElement"][-1]["name"] == "MoonshotAI: Kimi K2.6"
    assert "aggregateRating" not in service
    assert "review" not in service
    assert "hasMerchantReturnPolicy" not in service["offers"]
    assert "shippingDetails" not in service["offers"]


def test_dashboard_links_to_public_models_not_keyed_api_catalog(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    # Core invariant: the homepage links to the PUBLIC models page, never the
    # keyed API catalog.
    assert 'href="/models"' in response.text
    assert 'href="https://api.trustedrouter.com/v1/models"' not in response.text
    # Redesigned homepage (2026-06): a static routing-diagram hero replaces the
    # animated orbital scene, on the friend-provided modern layout. Assert the
    # new conversion surface rather than the old orbital-scene markup.
    assert "Private, reliable LLM routing" in response.text  # new H1
    assert "ATTESTED GATEWAY" in response.text  # routing diagram
    assert "Get API key" in response.text  # primary CTA
    assert "Provider failover" in response.text  # hero proof row
    assert "data_collection" in response.text  # privacy-level routing pref
    assert 'href="/eu"' in response.text


def test_eu_host_renders_eu_landing_page(client: TestClient) -> None:
    response = client.get("/", headers={"host": "eu.trustedrouter.com"})

    assert response.status_code == 200
    assert "Use the EU gateway and an EU-focused model alias." in response.text
    assert "https://api-europe-west4.quillrouter.com/v1" in response.text


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
