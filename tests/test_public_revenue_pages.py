from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.catalog import endpoints_for_model
from trusted_router.dashboard import PUBLIC_PAGES
from trusted_router.storage import STORE


def test_revenue_pages_are_public(client: TestClient) -> None:
    markers = {
        "/compare/openrouter": "OpenRouter, but you can verify the prompt path.",
        "/compare/vercel-ai-gateway": "Use Vercel where it fits.",
        "/compare/litellm": "LiteLLM and TrustedRouter fit in the same stack.",
        "/docs/migrate-from-openrouter": "Change base_url",
        "/docs/synth": "Run a panel of models inside the attested gateway.",
        "/synth": "Synthesize many models into one perfect frontier answer.",
        "/resources": "Guides, comparisons, privacy references",
        "/customers/robot-robot-human": "From first call to production-scale legal AI in three weeks.",
        "/careers": "Work on attested AI routing",
        "/blog": "TrustedRouter blog",
        "/blog/they-are-still-training-on-your-data": (
            "They Are Still Training on Your Data"
        ),
        "/blog/no-log-is-a-promise-attestation-is-proof": (
            "ZDR is a vague promise. Attestation is precise proof"
        ),
        "/blog/fusion-evals-open-source": "New SOTA: TrustedRouter Synth beats Fable and Frontier",
        "/blog/sign-in-with-trustedrouter": "Sign in with TrustedRouter",
        "/sign-in-with-trustedrouter": "A complete user-funded AI flow.",
        "/security": "No prompt or output logs",
        "/eu": "Use the EU gateway and an EU-focused model alias.",
        # SEO landing pages — each targets a high-intent buyer query.
        # The marker is a load-bearing headline from the page so a
        # silent template breakage gets caught here.
        "/openrouter-alternative": "Switch from OpenRouter in one base URL.",
        "/private-llm-api": 'A private LLM API where "private" means cryptographically verified.',
        "/hipaa-llm-api": "The LLM API whose privacy posture is verifiable",
        "/llm-zero-data-retention": "Zero data retention as a verifiable property",
        "/claude-api-privacy": "Call Claude through a prompt path you can verify.",
        # Round-2 competitor-alternative + category pages.
        "/litellm-alternative": "LiteLLM lets you self-host.",
        "/portkey-alternative": "Portkey logs every request.",
        "/confidential-computing-llm": "Run LLM inference behind hardware attestation",
        "/badge": "Show where your customers' AI data goes.",
        "/tinfoil-alternative": "Same verifiable-privacy bet.",
        "/openai-compatible-llm-api": "Keep the SDK. Change the base URL.",
        "/kimi-k2-api": "Kimi K2 with provider fallback and measured routes.",
        "/gemini-flash-alternative": "Compare Gemini Flash with the cheapest good routes.",
        "/llm-provider-latency-benchmarks": "Provider speed data from real routed requests.",
        # SEO wave 2 — keyword-gap landing pages.
        "/azure-openai-alternative": "Azure OpenAI promises privacy in a contract; TrustedRouter proves it with hardware attestation you can check live.",
        "/deepseek-api-privacy": "DeepSeek V4 on attested infrastructure: your prompts never reach the model vendor.",
        "/glm-5-api": "Run GLM-5 and GLM-5.2 on attested hardware without sending your prompts to the model vendor.",
        "/gdpr-compliant-llm-api": "An LLM API built for GDPR workflows: attested inference, a signable DPA, and no prompt or output logs.",
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
        "/anonymous-llm-api": "Fund 220+ model routes from a crypto wallet, no card and no KYC, then verify the real-time content-stateless path.",
        "/cline-api-provider": "Your coding agent streams your entire repo through its API provider, so pick one you can verify.",
        "/sillytavern-api": "Point SillyTavern at an API that never logs prompts or outputs and proves what it runs.",
        "/aws-bedrock-alternative": "Keep the privacy you chose Bedrock for, without the quota wall.",
        "/llm-document-processing": "You do not need on-prem inference to keep your documents private.",
        "/gpt-oss-120b-api": "gpt-oss-120b, served fast on Cerebras and attested down to the image digest.",
        "/eu-ai-act-llm-compliance": "Your EU AI Act compliance file depends on facts from your LLM API vendor, and attestation makes those facts checkable.",
        "/x402-llm-api": "Your agent gets a 402, signs a payment, retries the call, and reads the completion.",
        "/confidential-cowork": "Confidentiality cannot be clicked away",
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


def test_rrh_customer_story_scopes_privacy_claims_and_uses_tailored_og(
    client: TestClient,
) -> None:
    response = client.get("/customers/robot-robot-human")

    assert response.status_code == 200
    assert "Gateway attestation and provider guarantees answer different questions." in response.text
    assert "TrustedRouter did not inspect prompt or output content." in response.text
    assert "independently verified E2E routes" in response.text
    assert "not a single page ever leaving" not in response.text.lower()
    card = "https://trustedrouter.com/static/og/rrh-case-study.png"
    assert f'property="og:image" content="{card}"' in response.text
    assert f'name="twitter:image" content="{card}"' in response.text
    assert client.get("/static/og/rrh-case-study.png").status_code == 200


def test_public_pages_never_weaken_content_handling_to_a_default(
    client: TestClient,
) -> None:
    paths = tuple(
        sorted(
            {
                "/",
                "/privacy",
                "/legal",
                "/legal/dpa",
                "/legal/baa",
                *(f"/{key}" for key in PUBLIC_PAGES),
            }
        )
    )
    weak_claims = (
        "prompt logs by default",
        "prompt storage by default",
        "prompt or output content by default",
        "prompt or output storage by default",
        "prompts by default",
        "nothing stored by default",
        "nothing kept by default",
        "content storage is opt-in",
    )

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        rendered = response.text.lower()
        for claim in weak_claims:
            assert claim not in rendered, f"{path} contains weak claim {claim!r}"


def test_signup_grant_amount_is_not_advertised(client: TestClient) -> None:
    for path in [
        "/",
        "/pricing",
        "/sign-in-with-trustedrouter",
        "/blog/sign-in-with-trustedrouter",
    ]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        rendered = response.text.lower()
        assert "$0.10" not in rendered
        assert "$0.30" not in rendered
        assert "ten cents" not in rendered

    for path in [
        Path("docs/sign-in-with-trustedrouter.md"),
        Path("src/trusted_router/templates/console/welcome.html"),
    ]:
        source = path.read_text().lower()
        assert "$0.10" not in source
        assert "$0.30" not in source
        assert "ten cents" not in source


def test_revenue_pages_support_link_checkers(client: TestClient) -> None:
    paths = [
        "/compare/openrouter",
        "/compare/vercel-ai-gateway",
        "/compare/litellm",
        "/docs/migrate-from-openrouter",
        "/docs/synth",
        "/synth",
        "/blog",
        "/blog/they-are-still-training-on-your-data",
        "/blog/no-log-is-a-promise-attestation-is-proof",
        "/blog/fusion-evals-open-source",
        "/security",
        "/badge",
        "/eu",
        "/models",
    ]

    for path in paths:
        assert client.head(path).status_code == 200
        slash_response = client.get(f"{path}/", follow_redirects=False)
        assert slash_response.status_code == 200


def test_confidential_ai_badge_is_embeddable_and_scoped(client: TestClient) -> None:
    response = client.get("/badge")
    assert response.status_code == 200
    assert "Confidential AI" in response.text
    assert 'model="trustedrouter/confidential"' in response.text
    assert 'provider.min_privacy="confidential"' in response.text
    assert "not a SOC 2, ISO 27001, HIPAA, or product-wide certification" in response.text
    assert "https://trustedrouter.com/static/badges/confidential-ai-light.svg" in response.text
    assert "https://trustedrouter.com/static/badges/confidential-ai-dark.svg" in response.text
    assert "certified confidential" not in response.text.casefold()
    card = "https://trustedrouter.com/static/og/confidential-ai-badge.png"
    assert f'property="og:image" content="{card}"' in response.text
    assert f'name="twitter:image" content="{card}"' in response.text
    assert "TrustedRouter Confidential AI trust seal with hardware attestation" in response.text
    assert client.get("/static/og/confidential-ai-badge.png").status_code == 200

    for asset in (
        "/static/badges/confidential-ai-light.svg",
        "/static/badges/confidential-ai-dark.svg",
        "/static/badges/confidential-ai-seal.svg",
        "/static/badges/confidential-ai-light.png",
        "/static/badges/confidential-ai-dark.png",
        "/static/badges/confidential-ai-seal.png",
    ):
        badge = client.get(asset)
        assert badge.status_code == 200, asset
        assert "public" in badge.headers["cache-control"]


def test_confidential_cowork_is_self_serve_and_fail_closed(client: TestClient) -> None:
    response = client.get("/confidential-cowork")

    assert response.status_code == 200
    assert "Confidential Cowork by TrustedRouter" in response.text
    assert "Confidential-Cowork-macOS-universal.dmg" in response.text
    assert "trustedrouter/confidential" in response.text
    assert "searchable catalog of specific models and providers" in response.text
    assert "Default route</span><strong>trustedrouter/confidential" in response.text
    assert "Data collection</span><strong>deny" in response.text
    assert "United States or European Union" in response.text
    assert "No eligible confidential provider means no model request" in response.text
    assert "Plan an enterprise deployment" in response.text
    screenshot = client.get("/static/confidential-cowork-desktop.png")
    assert screenshot.status_code == 200
    assert screenshot.headers["content-type"] == "image/png"

    canonical_mark = 'd="M1.4 7.5H6.5L11 12M1.4 16.5H4L8.5 12M1.4 12H15.5"'
    for asset in (
        "/static/badges/confidential-ai-light.svg",
        "/static/badges/confidential-ai-dark.svg",
        "/static/badges/confidential-ai-seal.svg",
    ):
        assert canonical_mark in client.get(asset).text

    homepage = client.get("/")
    assert 'href="/badge">Confidential AI badge</a>' in homepage.text

    security = client.get("/security")
    assert 'href="/badge">Get the badge</a>' in security.text
    assert "Chutes requests are encrypted to the measured GPU workload" in security.text
    assert "nras.attestation.nvidia.com" in security.text
    assert "api.trustedservices.intel.com" in security.text
    assert "never falls back to plaintext Chutes transport" in security.text


def test_blog_has_no_phd_hiring_banner(client: TestClient) -> None:
    for path in [
        "/blog",
        "/blog/they-are-still-training-on-your-data",
        "/blog/no-log-is-a-promise-attestation-is-proof",
        "/blog/sign-in-with-trustedrouter",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert "hiring-banner" not in response.text
        assert "PhD researchers" not in response.text
        assert "We're hiring" not in response.text


def test_public_pricing_matches_five_point_five_percent_billing_policy(
    client: TestClient,
) -> None:
    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    assert "provider cost + 5.5%" in pricing.text
    assert "direct provider quote + 20%" in pricing.text
    assert "Cheaper. Smarter. More reliable. More secure." in pricing.text
    assert "5.5% pay as you go fee on credit purchases" in pricing.text
    assert 'href="https://openrouter.ai/pricing"' in pricing.text
    assert "10% markup" not in pricing.text

    comparison = client.get("/compare/openrouter")
    assert comparison.status_code == 200
    assert "5.5% on prepaid model cost" in comparison.text
    # The fee bases differ and the page now says so: OpenRouter's 5.5%
    # ($0.80 min) applies when buying credits with inference at list price;
    # ours applies to prepaid model cost.
    assert "5.5% ($0.80 min) buying credits; inference at list price" in comparison.text
    assert "Provider cost + 5.5% markup" in comparison.text
    assert "10% markup" not in comparison.text

    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert "Text and embedding prepaid pricing is provider cost + 5.5%" in llms.text
    assert "Video generation is the direct provider quote + 20%" in llms.text


def test_router_comparisons_publish_current_catalog_and_composable_litellm_stack(
    client: TestClient,
) -> None:
    litellm = client.get("/compare/litellm")
    litellm_seo = client.get("/litellm-alternative")
    openrouter = client.get("/compare/openrouter")

    assert litellm.status_code == litellm_seo.status_code == openrouter.status_code == 200
    assert "LiteLLM and TrustedRouter fit in the same stack." in litellm.text
    assert "Direct provider A" in litellm.text
    assert "TrustedRouter completes the map" in litellm.text
    assert "600+ additional models" in litellm.text
    assert "Use LiteLLM in front of TrustedRouter." in litellm_seo.text
    assert "600+ model ids across 80+ providers" in openrouter.text
    assert "TrustedRouter's public API lists 600+ model ids" in openrouter.text


def test_paid_search_landing_pages_drive_a_runnable_first_call(
    client: TestClient,
) -> None:
    openai_page = client.get("/openai-compatible-llm-api")
    assert openai_page.status_code == 200
    assert "Keep the SDK. Change the base URL." in openai_page.text
    assert "import os" in openai_page.text
    assert 'model="trustedrouter/cheap"' in openai_page.text
    assert 'data-action="copy-code"' in openai_page.text
    assert openai_page.text.count("Create my API key") == 2
    assert "No card required." in openai_page.text
    assert '<a class="btn primary" href="/docs">' not in openai_page.text

    migration_page = client.get("/openrouter-alternative")
    assert migration_page.status_code == 200
    assert "Switch from OpenRouter in one base URL." in migration_page.text
    assert "import os" in migration_page.text
    assert migration_page.text.count("Create my API key") == 2
    assert "No card required." in migration_page.text
    assert '<a class="btn primary" href="/chat">' not in migration_page.text


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
    assert "trustedrouter/confidential" in response.text
    assert "trustedrouter/fast" in response.text
    assert "trustedrouter/eu" in response.text
    assert "trustedrouter/synth" in response.text
    assert "trustedrouter/advisor" in response.text
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
    assert '<meta name="robots" content="noindex,follow">' in response.text
    assert '<link rel="canonical" href="https://trustedrouter.com/choose">' in response.text
    assert "Choose with route-level facts." in response.text
    assert "Upstream privacy floor" in response.text
    assert 'id="providerCount"' in response.text
    assert "/static/choose-app.css?v=2" in response.text
    assert "/static/choose-app.js?v=4" in response.text
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
        "TrustedRouter stores billing and route metadata, never prompt/output content."
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
    for slug, name in (("kimi", "Kimi"), ("parasail", "Parasail"), ("tinfoil", "Tinfoil")):
        assert f'href="/providers/{slug}" title="{slug}"' in response.text
        assert f'src="/static/provider-logos/{slug}.png"' in response.text
        assert f"<span>{name}</span>" in response.text
    assert 'href="https://aiiq.org/models/kimi-k2.6/"' in response.text
    assert "IQ 116" in response.text


def test_public_models_page_is_a_ranked_searchable_price_explorer(
    client: TestClient,
) -> None:
    response = client.get("/models")

    assert response.status_code == 200
    body = response.text
    assert 'type="search"' in body
    assert 'data-model-search' in body
    assert 'data-model-sort' in body
    assert "Cached input" in body
    assert "/static/models.js" in body
    assert 'href="/for-developers"' in body
    assert 'href="/docs/quickstart"' not in body
    assert 'data-endpoints-url="/v1/models/z-ai/glm-5.3-flash/endpoints"' in body

    glm = body.index('data-model-id="z-ai/glm-5.3-flash"')
    kimi = body.index('data-model-id="moonshotai/kimi-k3"')
    deepseek = body.index('data-model-id="deepseek/deepseek-v4-pro-0813"')
    router_alias = body.index('data-model-id="trustedrouter/auto"')
    assert glm < kimi < deepseek < router_alias


def test_public_model_detail_lists_distinct_serving_providers(client: TestClient) -> None:
    model_id = "moonshotai/kimi-k2.6"
    response = client.get(f"/models/{model_id}")

    assert response.status_code == 200
    assert "Providers serving this model" in response.text
    assert "Endpoints</th>" in response.text
    assert 'href="https://aiiq.org/models/kimi-k2.6/"' in response.text
    assert "IQ 116" in response.text
    expected_providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
    assert "kimi" in expected_providers
    for provider in expected_providers:
        assert f'title="{provider}"' in response.text


def test_public_partner_model_discloses_fixed_price_and_minimum(
    client: TestClient,
) -> None:
    response = client.get("/models/parasail/liberty-2.0")

    assert response.status_code == 200
    assert "$2/1M tokens" in response.text
    assert "$19/1M tokens" in response.text
    assert "$0.001 per successful request" in response.text


def test_public_model_pages_never_claim_tr_stores_content(client: TestClient) -> None:
    catalog = client.get("/models")
    detail = client.get("/models/moonshotai/kimi-k2.6")

    assert catalog.status_code == 200
    assert detail.status_code == 200
    assert "stores content" not in catalog.text.lower()
    assert "tr stores content" not in detail.text.lower()
    assert "Provider policy" in detail.text
    assert "varies by provider" in catalog.text


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
    moonshot_row = re.search(
        r'<tr>\s*<td><a[^>]+href="/providers/kimi".*?</tr>',
        detail.text,
        flags=re.DOTALL,
    )
    assert moonshot_row is not None
    assert "privacy unknown" in moonshot_row.group(0)
    assert "provider E2EE" not in moonshot_row.group(0)


def test_single_provider_model_shows_provider_posture_not_variation(
    client: TestClient,
) -> None:
    detail = client.get("/models/qwen/qwen-2.5-72b-instruct")

    assert detail.status_code == 200
    assert "Novita AI" in detail.text
    assert "privacy unknown" in detail.text
    assert "varies by provider" not in detail.text
    assert "varies by route" not in detail.text


def test_phala_pages_do_not_claim_verified_provider_e2ee(client: TestClient) -> None:
    provider = client.get("/providers/phala")
    detail = client.get("/models/z-ai/glm-5.2")

    assert provider.status_code == 200
    assert 'Provider E2EE</th><td><span class="pill ">no</span>' in provider.text
    assert "does not yet verify the complete receipt chain" in provider.text
    assert detail.status_code == 200
    assert "provider E2EE not verified" in detail.text


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
    assert 'Canonical: <a href="/models/trustedrouter/socrates-2.0"' in rolling.text


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
        r'<script type="application/ld\+json"[^>]*>(?P<payload>.*?)</script>',
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
    assert "600+ AI Models at your fingertips." in response.text
    assert "One Unified Interface." in response.text
    assert "Privacy with proof." in response.text
    assert "Better privacy, better prices, better uptime, no subscriptions." in response.text
    assert '<strong>81+</strong><span>providers</span>' in response.text
    assert '<strong>3 clouds</strong><span>GCP · AWS · Azure</span>' in response.text
    assert 'class="region-map-card"' not in response.text
    assert "Provable privacy." not in response.text
    assert "ATTESTED GATEWAY" not in response.text
    assert 'class="hero-sdk-swap"' in response.text
    assert 'base_url="https://api.openai.com/v1"' in response.text
    assert f'base_url="{client.app.state.settings.api_base_url}"' in response.text
    assert 'src="/static/trustedrouter-explainer.jpg"' in response.text
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
    assert "<title>API Docs: Quickstart and SDKs | TrustedRouter</title>" in docs.text
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
