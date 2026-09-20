from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS, NATIVE_DECISION_MODEL_IDS


def test_decide_docs_publish_the_contract(client: TestClient) -> None:
    response = client.get("/docs/decide")

    assert response.status_code == 200
    text = response.text
    assert "/decide" in text and "POST /v1/evaluate" in text
    for field in ('"state"', '"questions"', '"answers"', '"probabilities"', '"inputTokens"'):
        assert field in text, field
    for question_type in ("boolean", "choice", "score"):
        assert f'<span class="card-label">{question_type}</span>' in text
    assert "trustedrouter/trev-1.0" in text
    assert "typesafe-ai/jev" in text


def test_decide_docs_list_exactly_the_models_the_catalog_advertises(client: TestClient) -> None:
    """The model table is hand-written; this keeps it from drifting from the
    catalog, which is what /v1/models and the gateway actually serve."""
    text = client.get("/docs/decide").text
    for model_id in (*NATIVE_DECISION_MODEL_IDS, "typesafe-ai/jev"):
        assert MODELS[model_id].supports_decide or model_id in NATIVE_DECISION_MODEL_IDS
        assert f'<span class="mono">{model_id}</span>' in text, (
            f"{model_id} missing from the docs table"
        )
    assert text.count('<tr><td><span class="mono">') - 4 == len(NATIVE_DECISION_MODEL_IDS) + 1, (
        "the docs table lists a model the catalog does not advertise"
    )


def test_decide_docs_state_the_guarantee_and_its_cost_honestly(client: TestClient) -> None:
    text = client.get("/docs/decide").text
    # The guarantee.
    assert "never a guess" in text
    assert "Hosted models are checked exactly as chat models are" in text
    # What it costs the caller, stated rather than buried.
    assert "Both attempts consumed tokens at the model host and both are billed" in text
    # What the numbers are and are not.
    assert "The eval is small and deliberately easy" in text
    assert "your request also pays for authorization and settlement through the gateway" in text
    assert "hosts vary from run to run" in text
    # Where the state goes: the vendor directly, and the one case where it
    # does not. The fallback is disclosed, not discovered.
    assert "called at TypeSafe's own API" in text
    assert "fail over to Vercel AI Gateway" in text
    assert "cross two third parties" in text
    # A client must be told when retrying would pay twice.
    assert "x-should-retry: false" in text


def test_decide_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/decide"' in client.get("/docs").text
    assert 'href="/docs/decide"' in client.get("/").text
    assert "/docs/decide" in client.get("/llms.txt").text
    assert "/docs/decide" in client.get("/docs/llms.txt").text
    assert "/docs/decide" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/decide</loc>" in client.get("/sitemap-core.xml").text
    )


def test_decide_docs_lead_with_the_alpha_path_and_promise_every_alias(client: TestClient) -> None:
    """The route's name is POST /api/alpha/decide. Every other path the gateway
    answers to is an alias, listed once, from the same table the rest of the
    site reads -- so a page cannot keep advertising a path as the default after
    the default moves."""
    from trusted_router.catalog_data import DECIDE_PATH, DECIDE_PATH_ALIASES, decide_url

    assert DECIDE_PATH == "/api/alpha/decide"
    # Beside /v1 on the API host, not under it, whatever the base URL looks like.
    for base in ("https://api.trustedrouter.com/v1", "https://api.trustedrouter.com/v1/"):
        assert decide_url(base) == "https://api.trustedrouter.com/api/alpha/decide"
    assert decide_url("http://localhost:8000") == "http://localhost:8000/api/alpha/decide"

    text = client.get("/docs/decide").text
    assert f"<code>POST {DECIDE_PATH}</code>" in text
    assert "/api/alpha/decide \\" in text, "the curl example calls the default path"
    assert "/v1/decide \\" not in text, "an example still calls the old default"
    assert [path for path, _ in DECIDE_PATH_ALIASES] == [
        "/api/decide",
        "/v1/decide",
        "/v1/evaluate",
        "/api/alpha/decisions",
        "/api/decisions",
    ], "mirrors isDecidePath in the attested gateway; change both together"
    for path, _ in DECIDE_PATH_ALIASES:
        assert f"<code>POST {path}</code>" in text, path
    # Callers coming from OpenRouter or TypeSafe: their spelling, their shape.
    assert "&#34;type&#34;: &#34;noul&#34;" in text or '"type": "noul"' in text
    assert "typesafe/jev-1.13" in text


def test_every_page_that_shows_a_decide_call_uses_the_default_path(client: TestClient) -> None:
    for path in (
        "/docs",
        "/models/trustedrouter/mev-1.0/api",
        "/compare/models/trustedrouter/mev-1.0/vs/trustedrouter/trev-1.0",
    ):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text[:200])
        assert "/api/alpha/decide" in page.text, path
        assert "/v1/decide" not in page.text, path


def test_no_template_names_the_decide_path_by_hand() -> None:
    """The path moved once already, and a page that spelled it out kept
    advertising the old one. /docs/decide may LIST /v1/decide, and does so from
    DECIDE_PATH_ALIASES; no template writes either path as a literal."""
    from pathlib import Path

    offenders = [
        str(template)
        for template in Path("src/trusted_router/templates").rglob("*.html")
        if any(literal in template.read_text() for literal in ("/v1/decide", "/api/alpha/decide"))
    ]
    assert offenders == []

