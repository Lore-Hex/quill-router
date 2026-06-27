from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from trusted_router.routes.public import INDEXNOW_KEY


def test_robots_and_sitemap_are_public(client: TestClient) -> None:
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://trustedrouter.com/sitemap.xml" in robots.text
    assert "Disallow: /console" in robots.text
    assert "Disallow: /v1/" in robots.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<sitemapindex" in sitemap.text
    for child in [
        "/sitemap-core.xml",
        "/sitemap-providers.xml",
        "/sitemap-models.xml",
        "/sitemap-comparisons.xml",
    ]:
        assert f"<loc>https://trustedrouter.com{child}</loc>" in sitemap.text
    assert "<lastmod>" in sitemap.text

    core = client.get("/sitemap-core.xml")
    assert core.status_code == 200
    assert "<urlset" in core.text
    assert "<loc>https://trustedrouter.com/eu</loc>" in core.text
    assert "<loc>https://trustedrouter.com/openai-compatible-llm-api</loc>" in core.text
    assert "<loc>https://trustedrouter.com/kimi-k2-api</loc>" in core.text
    assert "<loc>https://trustedrouter.com/gemini-flash-alternative</loc>" in core.text
    assert "<loc>https://trustedrouter.com/llm-provider-latency-benchmarks</loc>" in core.text
    assert "<loc>https://trustedrouter.com/blog</loc>" in core.text
    assert "<loc>https://trustedrouter.com/llms.txt</loc>" in core.text
    assert "<loc>https://trustedrouter.com/blog/frontier-fusion-mythos-target</loc>" not in core.text
    assert "<loc>https://trustedrouter.com/blog/fusion-evals-open-source</loc>" in core.text
    assert "<loc>https://trustedrouter.com/docs/synth</loc>" in core.text
    assert "<loc>https://trustedrouter.com/docs/fusion</loc>" in core.text

    models = client.get("/sitemap-models.xml")
    assert models.status_code == 200
    assert models.text.count("<url>") >= 200
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3</loc>" in models.text
    # minimax-m3 now has cited benchmark scores, so its /benchmarks page is indexed.
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/benchmarks</loc>" in models.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/providers</loc>" in models.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/pricing</loc>" in models.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/uptime</loc>" not in models.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/api</loc>" not in models.text

    providers = client.get("/sitemap-providers.xml")
    assert providers.status_code == 200
    assert "<loc>https://trustedrouter.com/providers/minimax</loc>" in providers.text

    comparisons = client.get("/sitemap-comparisons.xml")
    assert comparisons.status_code == 200
    assert (
        "<loc>https://trustedrouter.com/compare/models/moonshotai/kimi-k2.6/vs/z-ai/glm-5.1</loc>"
        in comparisons.text
    )
    combined = sitemap.text + core.text + models.text + providers.text + comparisons.text
    assert "trustedrouter/monitor" not in combined
    assert "openrouter.ai" not in combined


def test_indexnow_key_file_is_public(client: TestClient) -> None:
    response = client.get(f"/{INDEXNOW_KEY}.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.strip() == INDEXNOW_KEY
    assert "cache-control" in response.headers


def test_llms_text_files_are_public_and_do_not_leak_secret_material(
    client: TestClient,
) -> None:
    for path in ["/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "TrustedRouter" in response.text
        assert "api.trustedrouter.com/v1" in response.text
        assert "trustedrouter/eu" in response.text
        assert "trustedrouter/synth" in response.text
        assert "trustedrouter/iris" in response.text
        assert "trustedrouter/prometheus" in response.text
        assert "trustedrouter/zeus" in response.text
        assert "trustedrouter/prometheus-code" in response.text
        assert "trustedrouter/iris-1.0" in response.text
        assert "trustedrouter/prometheus-1.0" in response.text
        assert "trustedrouter/zeus-1.0" in response.text
        assert "trustedrouter/prometheus-code-1.0" in response.text
        assert "https://trustedrouter.com/docs/synth" in response.text
        assert "https://trustedrouter.com/blog" in response.text
        assert "OpenAI compatible" in response.text or "OpenAI-compatible" in response.text
        assert "sk-tr-v1-" not in response.text
        assert "BEGIN PRIVATE KEY" not in response.text
    root_llms = client.get("/llms.txt")
    assert "Best Short Answer" in root_llms.text
    assert "OpenRouter alternative" in root_llms.text
    assert "lower-cost open-weight models" in root_llms.text
    assert "trustedrouter/e2e" in root_llms.text


def test_homepage_has_plain_llm_seo_positioning(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Why developers choose TrustedRouter." in response.text
    assert "OpenRouter alternative" in response.text
    assert "Better trust" in response.text
    assert "Cheaper routes" in response.text
    assert "Faster migration" in response.text
    assert "More reliable inference" in response.text
    assert 'href="/llms.txt"' in response.text


def test_public_provider_route_defaults_to_html_for_link_checkers(client: TestClient) -> None:
    response = client.get("/providers")

    assert response.status_code == 200
    assert "<title>Providers | TrustedRouter</title>" in response.text
    assert "Provider transparency" in response.text
    assert "application/json" not in response.headers["content-type"]

    json_response = client.get("/providers", headers={"accept": "application/json"})
    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")


def test_public_structured_data_covers_lists_datasets_and_faqs(client: TestClient) -> None:
    models = client.get("/models")
    assert models.status_code == 200
    models_payload = _json_ld(models.text)
    models_types = {item["@type"] for item in models_payload["@graph"]}
    assert {"BreadcrumbList", "ItemList"}.issubset(models_types)

    leaderboard = client.get("/leaderboard")
    assert leaderboard.status_code == 200
    leaderboard_payload = _json_ld(leaderboard.text)
    leaderboard_types = {item["@type"] for item in leaderboard_payload["@graph"]}
    assert {"BreadcrumbList", "Dataset"}.issubset(leaderboard_types)

    faq = client.get("/openai-compatible-llm-api")
    assert faq.status_code == 200
    faq_payload = _json_ld(faq.text)
    faq_types = {item["@type"] for item in faq_payload["@graph"]}
    assert {"BreadcrumbList", "FAQPage"}.issubset(faq_types)
    assert "Can I keep using the OpenAI SDK?" in faq.text

    blog = client.get("/blog/fusion-evals-open-source")
    assert blog.status_code == 200
    blog_payload = _json_ld(blog.text)
    blog_types = {item["@type"] for item in blog_payload["@graph"]}
    assert {"BreadcrumbList", "BlogPosting"}.issubset(blog_types)
    assert "DRACO" in blog.text
    assert "TrustedRouter Synth Draco on GitHub" in blog.text

    removed_blog = client.get("/blog/frontier-fusion-mythos-target")
    assert removed_blog.status_code == 404
    assert "Chasing Mythos-level Synth" not in removed_blog.text


def test_blog_index_shows_scannable_post_images(client: TestClient) -> None:
    response = client.get("/blog")
    assert response.status_code == 200
    assert 'class="blog-thumb"' in response.text
    assert 'src="https://trustedrouter.com/static/og/blog/fusion-is-two-jobs.png"' in response.text
    assert 'alt="Synth is two jobs, and no model wins both visual summary"' in response.text
    assert 'href="/blog/fusion-is-two-jobs"' in response.text
    assert response.text.count('class="blog-thumb"') >= 10


def _json_ld(html: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/ld\+json">(?P<payload>.*?)</script>',
        html,
    )
    assert match is not None
    payload = json.loads(match.group("payload"))
    assert isinstance(payload, dict)
    return payload


def test_public_legal_packet_exposes_procurement_checkpoint(client: TestClient) -> None:
    page = client.get("/legal")
    assert page.status_code == 200
    assert "Legal and procurement packet" in page.text
    assert "Lore Hex Corp" in page.text
    assert "Joseph Perla" in page.text
    assert "security@trustedrouter.com" in page.text
    assert "Delaware C Corporation" in page.text
    assert "41-5339728" in page.text
    assert "144992055" in page.text
    assert "Not yet published" not in page.text
    assert "DPA" in page.text
    assert "draft_available_requires_signature" in page.text
    assert "SOC_2" in page.text
    assert "not_obtained" in page.text
    assert "trust.trustedrouter.com" in page.text

    packet = client.get("/legal/procurement.json")
    assert packet.status_code == 200
    assert packet.headers["content-type"].startswith("application/json")
    data = packet.json()
    assert data["legal_entity"]["name"] == "Lore Hex Corp"
    assert data["legal_entity"]["type"] == "Delaware C Corporation"
    assert data["legal_entity"]["signatory_name"] == "Joseph Perla"
    assert data["legal_entity"]["signatory_title"] == "CEO"
    assert data["legal_entity"]["security_contact_email"] == "security@trustedrouter.com"
    assert data["legal_defaults"]["sensitive_workload_default_model"] == "trustedrouter/zdr"
    assert data["legal_defaults"]["content_export_requires_written_approval"] is True
    assert data["checkpoint"]["named_entity"]["obtained_for_production"] is True
    assert data["checkpoint"]["subprocessor_list"]["obtained_for_production"] is True
    assert data["checkpoint"]["DPA"]["obtained_for_production"] is False
    assert data["checkpoint"]["SOC_2"]["status"] == "not_obtained"
    assert data["checkpoint"]["SOC_2"]["obtained_for_production"] is False
    assert data["checkpoint"]["HIPAA"]["status"] == "readiness_package_available_requires_signed_baa"
    assert data["checkpoint"]["HIPAA"]["obtained_for_production"] is False
    assert "Do not send privileged attorney work product" in data["production_recommendation"]


def test_public_legal_dpa_baa_and_subprocessors_are_honest(client: TestClient) -> None:
    dpa = client.get("/legal/dpa")
    assert dpa.status_code == 200
    assert "Draft DPA" in dpa.text
    assert "Signature required" in dpa.text
    assert "Joseph Perla" in dpa.text
    assert "No prompt/output storage by TrustedRouter" in dpa.text
    assert "trustedrouter/zdr" in dpa.text
    assert "written approval" in dpa.text

    baa = client.get("/legal/baa")
    assert baa.status_code == 200
    assert "Draft BAA" in baa.text
    assert "Do not send PHI yet" in baa.text
    assert "not yet have HIPAA certification" in baa.text
    assert "Joseph Perla" in baa.text
    assert "security@trustedrouter.com" in baa.text

    subprocessors = client.get("/legal/subprocessors")
    assert subprocessors.status_code == 200
    assert "Platform subprocessors" in subprocessors.text
    assert "Model provider subprocessors" in subprocessors.text
    assert "Google Cloud Platform" in subprocessors.text
    assert "Anthropic" in subprocessors.text
    assert "OpenAI" in subprocessors.text
    assert "Policy source" in subprocessors.text

    subprocessors_json = client.get("/legal/subprocessors.json")
    assert subprocessors_json.status_code == 200
    payload = subprocessors_json.json()
    system_names = {row["name"] for row in payload["system_subprocessors"]}
    model_names = {row["name"] for row in payload["model_provider_subprocessors"]}
    assert {"Google Cloud Platform", "Stripe", "Sentry"}.issubset(system_names)
    assert {"Anthropic", "OpenAI", "Gemini"}.issubset(model_names)
    anthropic = next(row for row in payload["model_provider_subprocessors"] if row["id"] == "anthropic")
    assert anthropic["zdr"] is True


def test_public_soc2_and_hipaa_readiness_pages_are_explicitly_not_reports(
    client: TestClient,
) -> None:
    soc2 = client.get("/legal/soc2-readiness")
    assert soc2.status_code == 200
    assert "SOC 2 readiness" in soc2.text
    assert "No SOC 2 report yet" in soc2.text
    assert "not_obtained" in soc2.text
    assert "docs/compliance/soc2/control-matrix.md" in soc2.text
    assert "audited, certified, or Type I complete" in soc2.text

    soc2_json = client.get("/legal/soc2-readiness.json")
    assert soc2_json.status_code == 200
    assert soc2_json.headers["content-type"].startswith("application/json")
    soc2_payload = soc2_json.json()
    assert soc2_payload["soc2_type_1_report"] == "not_obtained"
    assert soc2_payload["soc2_type_2_report"] == "not_obtained"
    assert "Security" in soc2_payload["target_report"]["trust_services_categories"]
    assert len(soc2_payload["documents"]) >= 25
    assert all(not document["path"].endswith("/") for document in soc2_payload["documents"])
    assert any(
        document["path"] == "docs/compliance/soc2/evidence-checklist.md"
        for document in soc2_payload["documents"]
    )

    hipaa = client.get("/legal/hipaa-readiness")
    assert hipaa.status_code == 200
    assert "HIPAA readiness" in hipaa.text
    assert "No PHI until signed" in hipaa.text
    assert "docs/compliance/hipaa/hipaa-readiness-matrix.md" in hipaa.text
    assert "not an executed BAA" in hipaa.text

    hipaa_json = client.get("/legal/hipaa-readiness.json")
    assert hipaa_json.status_code == 200
    assert hipaa_json.headers["content-type"].startswith("application/json")
    hipaa_payload = hipaa_json.json()
    assert hipaa_payload["baa"] == "draft_available_requires_signature"
    assert hipaa_payload["phi_production_approved"] is False
    assert hipaa_payload["hipaa_certification"] == "not_obtained"
    assert hipaa_payload["contract_signatory"]["name"] == "Joseph Perla"
    assert hipaa_payload["security_contact_email"] == "security@trustedrouter.com"
    assert hipaa_payload["default_route_policy"]["default_sensitive_alias"] == "trustedrouter/zdr"
    assert hipaa_payload["default_route_policy"]["content_export_requires_written_approval"] is True
    assert len(hipaa_payload["documents"]) >= 8
    assert all(not document["path"].endswith("/") for document in hipaa_payload["documents"])
    assert "Executed BAA" in hipaa_payload["required_before_phi"]
    assert "signed" in hipaa_payload["agent_instruction"]


def test_provider_detail_page_links_served_models(client: TestClient) -> None:
    response = client.get("/providers/minimax")

    assert response.status_code == 200
    assert "<title>MiniMax Models | TrustedRouter</title>" in response.text
    assert "MiniMax M3" in response.text
    assert 'href="https://aiiq.org/models/minimax-m3/"' in response.text
    assert "IQ 109" in response.text
    assert "/models/minimax/minimax-m3/benchmarks" in response.text
    assert "Policy source" in response.text


def test_model_seo_cluster_pages_are_public_and_not_openrouter_links(
    client: TestClient,
) -> None:
    for section in ["benchmarks", "providers", "performance", "pricing", "uptime", "api"]:
        response = client.get(f"/models/minimax/minimax-m3/{section}")
        assert response.status_code == 200, response.text
        expected_label = "API" if section == "api" else section.title()
        assert f"MiniMax M3 {expected_label}" in response.text
        assert "openrouter.ai" not in response.text.lower()
        assert '<nav class="section-tabs"' in response.text
        assert 'href="https://aiiq.org/models/minimax-m3/"' in response.text
        assert "IQ 109" in response.text
        # benchmarks is indexable once a model has cited benchmark scores —
        # minimax-m3 now ships TrustedRouter SimpleQA/GSM8K/Aider rows.
        if section in {"providers", "pricing", "benchmarks"}:
            assert '<meta name="robots" content="noindex,follow">' not in response.text
            assert (
                f'<link rel="canonical" href="https://trustedrouter.com/models/minimax/minimax-m3/{section}">'
                in response.text
            )
        else:
            assert '<meta name="robots" content="noindex,follow">' in response.text
            assert (
                '<link rel="canonical" href="https://trustedrouter.com/models/minimax/minimax-m3">'
                in response.text
            )

    benchmarks = client.get("/models/minimax/minimax-m3/benchmarks")
    assert "MiniMax M3 model page" in benchmarks.text
    assert "LMArena leaderboard" in benchmarks.text
    assert "AI IQ profile" in benchmarks.text

    api = client.get("/models/minimax/minimax-m3/api")
    assert 'model="minimax/minimax-m3"' in api.text
    assert 'base_url="https://api.trustedrouter.com/v1"' in api.text


def test_model_comparison_pages_are_public(client: TestClient) -> None:
    response = client.get("/compare/models/moonshotai/kimi-k2.6/vs/z-ai/glm-5.1")

    assert response.status_code == 200
    assert "MoonshotAI: Kimi K2.6 vs Z.ai: GLM 5.1" in response.text
    assert "Compare routes" in response.text
    assert "Practical read" in response.text
    assert "cheapest route" in response.text
    assert 'href="https://aiiq.org/models/kimi-k2.6/"' in response.text
    assert "IQ 116" in response.text
    assert "/models/moonshotai/kimi-k2.6/pricing" in response.text
    assert "/models/z-ai/glm-5.1/providers" in response.text
    assert "openrouter.ai" not in response.text.lower()


def test_benchmarks_and_rankings_pages_link_model_clusters(client: TestClient) -> None:
    for path in ["/benchmarks", "/rankings"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "/models/minimax/minimax-m3/benchmarks" in response.text
        assert "/models/minimax/minimax-m3/performance" in response.text
        assert "/providers/minimax" in response.text
        assert 'href="https://aiiq.org/models/minimax-m3/"' in response.text
        assert "openrouter.ai" not in response.text.lower()


def test_first_body_image_picks_first_in_document_order() -> None:
    from trusted_router.dashboard import _first_body_image

    assert _first_body_image("<p>no imagery</p>") is None
    assert _first_body_image('<p>x</p><img src="/a.png" alt="a"><svg></svg>') == ("img", "/a.png")
    assert _first_body_image('<figure><svg viewBox="0 0 1 1"></svg></figure>') == ("svg", "")
    # whichever appears first wins
    assert _first_body_image('<svg></svg><img src="/b.png">')[0] == "svg"


def test_blog_post_og_image_uses_first_image_else_default(client: TestClient) -> None:
    # post that opens with an inline <svg> -> its rasterized card
    sota = client.get("/blog/fusion-evals-open-source")
    card = "https://trustedrouter.com/static/og/blog/fusion-evals-open-source.png"
    assert f'property="og:image" content="{card}"' in sota.text
    assert f'name="twitter:image" content="{card}"' in sota.text
    assert "static/og/blog/fusion-evals-open-source.png" in json.dumps(_json_ld(sota.text))
    assert client.get("/static/og/blog/fusion-evals-open-source.png").status_code == 200
    # the card alt is the post title, not the generic brand alt
    assert 'property="og:image:alt" content="New SOTA: TrustedRouter Synth beats Fable and Frontier"' in sota.text

    # The diagram sweep gave every post an OG diagram, so each post now uses its
    # OWN rasterized card (the imageless -> default brand-card path is covered by
    # the _first_body_image unit tests above).
    plain = client.get("/blog/the-models-that-say-no")
    plain_card = "https://trustedrouter.com/static/og/blog/the-models-that-say-no.png"
    assert f'property="og:image" content="{plain_card}"' in plain.text
