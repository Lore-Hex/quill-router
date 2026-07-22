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
        "/synth": "Synthesize many models into one perfect frontier answer.",
        "/resources": "Guides, comparisons, privacy references",
        "/careers": "Work on attested AI routing",
        "/blog": "TrustedRouter blog",
        "/blog/fusion-evals-open-source": "New SOTA: TrustedRouter Synth beats Fable and Frontier",
        "/security": "TrustedRouter does not store prompt or output content by default.",
        "/eu": "Use the EU gateway and an EU-focused model alias.",
        # SEO landing pages — each targets a high-intent buyer query.
        # The marker is a load-bearing headline from the page so a
        # silent template breakage gets caught here.
        "/openrouter-alternative": "An OpenRouter alternative built around verifiable privacy.",
        "/private-llm-api": 'A private LLM API where "private" means cryptographically verified.',
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
        # SEO wave 2 — keyword-gap landing pages.
        "/azure-openai-alternative": "Azure OpenAI promises privacy in a contract; TrustedRouter proves it with hardware attestation you can check live.",
        "/deepseek-api-privacy": "DeepSeek V4 on attested infrastructure: your prompts never reach the model vendor.",
        "/glm-5-api": "Run GLM-5 and GLM-5.2 on attested hardware without sending your prompts to the model vendor.",
        "/gdpr-compliant-llm-api": "An LLM API built for GDPR workflows: attested inference, a signable DPA, and no prompt storage by default.",
        "/chinese-ai-models-us-hosted": "Run GLM, Qwen, Kimi, and DeepSeek on US-hosted, attested infrastructure that never forwards a prompt to the model vendor.",
        "/minimax-m3-api": "MiniMax M3 on attested hardware, with prompts that never reach the model vendor.",
        "/best-llm-router": "The best LLM router is the one whose privacy claims you can verify with a curl command.",
        "/llm-failover": "Your uptime should not depend on one provider's status page.",
        "/groq-alternative": "Fast inference only counts when request 41 still goes through.",
        "/vertex-ai-alternative": "If Vertex AI is only your Gemini endpoint, you are maintaining a cloud platform to make an API call.",
        "/llm-api-for-financial-services": "An LLM API a bank risk committee can verify with one curl command.",
        "/llm-api-for-law-firms": "When the gateway operator provably cannot read the prompt, your privilege analysis starts from different facts.",
        "/llm-data-residency": "Residency pins where inference runs; attestation proves who can read the prompt.",
        "/no-log-llm-api": "No prompt logs, enforced by code you can read and attestation you can check.",
        "/anonymous-llm-api": "Fund 220+ model routes from a crypto wallet, no card and no KYC, then verify for yourself that prompts are not stored.",
        "/cline-api-provider": "Your coding agent streams your entire repo through its API provider, so pick one you can verify.",
        "/sillytavern-api": "Point SillyTavern at an API that stores no prompts by default and proves what it runs.",
        "/aws-bedrock-alternative": "Keep the privacy you chose Bedrock for, without the quota wall.",
        "/llm-document-processing": "You do not need on-prem inference to keep your documents private.",
        "/gpt-oss-120b-api": "gpt-oss-120b, served fast on Cerebras and attested down to the image digest.",
        "/eu-ai-act-llm-compliance": "Your EU AI Act compliance file depends on facts from your LLM API vendor, and attestation makes those facts checkable.",
        "/x402-llm-api": "Your agent gets a 402, signs a payment, retries the call, and reads the completion.",
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
        "/synth",
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


def test_public_pricing_matches_five_percent_billing_policy(client: TestClient) -> None:
    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    assert "provider cost + 5%" in pricing.text
    assert "Cheaper. Smarter. More reliable. More secure." in pricing.text
    assert "5.5% pay as you go fee on credit purchases" in pricing.text
    assert 'href="https://openrouter.ai/pricing"' in pricing.text
    assert "10% markup" not in pricing.text

    comparison = client.get("/compare/openrouter")
    assert comparison.status_code == 200
    assert "5% on prepaid model cost" in comparison.text
    assert "5.5% on credit purchases" in comparison.text
    assert "Provider cost + 5% markup" in comparison.text
    assert "10% markup" not in comparison.text

    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert "Prepaid pricing is provider cost + 5%" in llms.text


def test_agent_discovery_surfaces_model_advisor_skill(client: TestClient) -> None:
    for path in ["/", "/docs", "/docs/agent-setup"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Agent skill" in response.text or "model advisor playbook" in response.text
        assert "codex-skill" in response.text

    for path in ["/docs", "/docs/agent-setup"]:
        response = client.get(path)
        assert "https://github.com/Lore-Hex/LLM-advisor" in response.text
        assert (
            "https://raw.githubusercontent.com/Lore-Hex/LLM-advisor/main/SKILL.md" in response.text
        )

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
    assert "Choose with route-level facts." in response.text
    assert "Upstream privacy floor" in response.text
    assert "/static/choose-app.css?v=2" in response.text
    assert "/static/choose-app.js?v=2" in response.text
    assert "fonts.googleapis.com" not in response.text
    # Privacy floor defaults to Open (any provider), not ZDR.
    assert '<option value="0" selected>' in response.text

    script = client.get("/static/choose-app.js")
    assert script.status_code == 200
    assert 'const CATALOG_URL = "/choose/catalog.json"' in script.text
    assert "tr-choose-height" in script.text
    assert "/v1/models" not in script.text
    assert "/ai-iq/models.json" not in script.text
    assert "PROVIDER_LOOKUP" not in script.text
    assert "AI_IQ_LOOKUP" not in script.text
    assert "No upstream privacy floor is implied" not in response.text


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
    legacy = client.get("/fusion", follow_redirects=False)
    assert legacy.status_code == 301
    assert legacy.headers["location"] == "/synth"
    legacy_slash = client.get("/fusion/", follow_redirects=False)
    assert legacy_slash.status_code == 301
    assert legacy_slash.headers["location"] == "/synth"


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
    assert "judges with Kimi K2.7 Code" in response.text
    assert "synthesizes with GLM 5.2" in response.text
    assert "moonshotai/kimi-k2.7-code" in response.text
    assert "z-ai/glm-5.2" in response.text
    assert "minimax/minimax-m3" in response.text
    assert "google/gemma-4-31b-it" in response.text
    assert "deepseek/deepseek-v4-pro" in response.text
    assert "Final fallback can switch before the first byte" in response.text
    assert (
        "TrustedRouter stores billing and route metadata, not prompt/output content by default."
        in response.text
    )
    assert "OpenAI compatible API" in response.text
    legacy = client.get("/docs/fusion", follow_redirects=False)
    assert legacy.status_code == 301
    assert legacy.headers["location"] == "/docs/synth"


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
    for provider in ["kimi", "parasail", "together", "tinfoil", "novita"]:
        assert f'title="{provider}"' in response.text


def test_public_model_pages_never_claim_tr_stores_content(client: TestClient) -> None:
    catalog = client.get("/models")
    detail = client.get("/models/moonshotai/kimi-k2.6")

    assert catalog.status_code == 200
    assert detail.status_code == 200
    assert "stores content" not in catalog.text.lower()
    assert "tr stores content" not in detail.text.lower()
    assert "Provider policy" in detail.text
    assert "upstream varies" in detail.text


def test_public_kimi_k3_page_separates_router_attestation_from_provider_e2ee(
    client: TestClient,
) -> None:
    catalog = client.get("/models")
    detail = client.get("/models/moonshotai/kimi-k3")

    assert catalog.status_code == 200
    assert detail.status_code == 200
    assert "TR router attested" in catalog.text
    assert "TR router attestation verifies the\n      TrustedRouter gateway only" in detail.text
    assert "<th>TR router attested</th>" in detail.text
    assert "<th>Attested</th>" not in detail.text
    assert "Provider policy" in detail.text
    assert "upstream varies" in detail.text
    assert "provider E2EE" not in detail.text


def test_public_meta_model_detail_renders_orchestration_components(client: TestClient) -> None:
    response = client.get("/models/trustedrouter/socrates-1.1")

    assert response.status_code == 200
    assert "TrustedRouter Socrates 1.1" in response.text
    assert '<span class="pill">advisor</span>' in response.text
    assert '<span class="pill">named preset</span>' in response.text
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
    assert '<span class="pill">advisor</span>' in rolling.text
    assert '<span class="pill">rolling alias</span>' in rolling.text
    assert 'Canonical: <a href="/models/trustedrouter/socrates-1.1"' in rolling.text


def test_public_k3_combo_pages_render_exact_graphs(
    client: TestClient,
) -> None:
    prometheus = client.get("/models/trustedrouter/prometheus-2.0")
    assert prometheus.status_code == 200
    assert "TrustedRouter Prometheus 2.0" in prometheus.text
    assert "minimax/minimax-m3" in prometheus.text
    assert "moonshotai/kimi-k3" in prometheus.text
    assert "z-ai/glm-5.2" in prometheus.text
    assert "deepseek/deepseek-v4-pro" in prometheus.text
    assert "xiaomi/mimo-v2.5-pro" in prometheus.text

    openpatcher = client.get("/models/trustedrouter/openpatcher-g2")
    assert openpatcher.status_code == 200
    assert "TrustedRouter OpenPatcher-G2" in openpatcher.text
    assert "moonshotai/kimi-k3" in openpatcher.text
    assert "google/gemma-4-31b-it" in openpatcher.text
    assert "trustedrouter/prometheus-2.0" in openpatcher.text

    openpatcher_s2 = client.get("/models/trustedrouter/openpatcher-s2")
    assert openpatcher_s2.status_code == 200
    assert "TrustedRouter OpenPatcher-S2" in openpatcher_s2.text
    assert "moonshotai/kimi-k3" in openpatcher_s2.text
    assert "z-ai/glm-5.2" in openpatcher_s2.text

    iris = client.get("/models/trustedrouter/iris-2.0")
    assert iris.status_code == 200
    assert "TrustedRouter Iris 2.0" in iris.text
    assert "minimax/minimax-m3" in iris.text
    assert "moonshotai/kimi-k3" in iris.text
    assert "deepseek/deepseek-v4-pro" in iris.text

    plato = client.get("/models/trustedrouter/plato-pro-2.0")
    assert plato.status_code == 200
    assert "TrustedRouter Plato Pro 2.0" in plato.text
    assert "z-ai/glm-5.2" in plato.text
    assert "trustedrouter/prometheus-2.0" in plato.text


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
    assert "Every model." in response.text  # homepage tagline
    assert "Provable privacy." in response.text
    assert "ATTESTED GATEWAY" in response.text  # attestation record
    assert "Get API key" in response.text  # primary CTA
    assert "Provider failover" in response.text  # hero proof row
    assert 'min_privacy": "confidential"' in response.text
    assert 'href="/eu"' in response.text
    assert "/static/charter.css?v=" in response.text
    assert 'class="brand-mark"' in response.text


def test_public_docs_explain_hard_confidential_e2ee_filter(client: TestClient) -> None:
    docs = client.get("/docs")
    providers = client.get("/providers")
    agent_setup = client.get("/docs/agent-setup")

    assert docs.status_code == providers.status_code == agent_setup.status_code == 200
    assert '"min_privacy": "confidential"' in docs.text
    assert "<code>e2e</code> and <code>e2ee</code>" in docs.text
    assert "requires both provider-side confidential compute and end-to-end encryption" in docs.text
    assert "Unsupported model/provider combinations fail closed" in docs.text
    assert 'provider.min_privacy = "confidential"' in providers.text
    assert "these hard filters fail closed" in providers.text
    assert 'provider.min_privacy = "confidential"' in agent_setup.text


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
