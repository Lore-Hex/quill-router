from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.trust import trust_html


@pytest.mark.parametrize("url", ["/trust", "https://trust.trustedrouter.com/"])
def test_agent_verification_prompt_is_first_on_both_trust_surfaces(url: str) -> None:
    settings = Settings(environment="test", trust_gcp_image_digest="sha256:" + "00" * 32)
    with TestClient(create_app(settings, init_observability=False)) as client:
        response = client.get(url)
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    main = page.find("main")
    assert main is not None
    first = main.find("section", recursive=False)
    assert first is not None
    assert first.get("aria-labelledby") == "agent-verify-title"
    prompt = first.find(id="trust-agent-prompt")
    assert prompt is not None
    text = prompt.get_text()
    assert len(text.split()) <= 55
    for required in (
        "https://trustedrouter.com",
        "https://trust.trustedrouter.com",
        "fresh, TLS-bound attestation",
        "published source and build provenance",
        "model-provider privacy claims",
        "anything you cannot verify",
    ):
        assert required in text
    button = first.find("button", id="copy-trust-prompt")
    assert button is not None
    assert button.get("type") == "button"
    assert button.get("aria-controls") == "trust-agent-prompt"
    assert button.get("onclick") is None
    icon = button.find("img")
    assert icon is not None
    assert icon.get("alt") == "Copy"
    assert icon.get("aria-hidden") == "true"
    assert first.find(id="trust-copy-status").get("role") == "status"
    script = page.find("script", src="/static/trust-prompt.js")
    assert script is not None
    assert script.has_attr("defer")


@pytest.mark.parametrize("status", ["stale", "unavailable"])
def test_prompt_keeps_release_failures_visible(status: str) -> None:
    page = BeautifulSoup(
        trust_html(Settings(environment="test"), release_metadata_status=status),
        "html.parser",
    )
    sections = page.find("main").find_all("section", recursive=False)
    assert sections[0].find(id="trust-agent-prompt") is not None
    assert "warn" in sections[1].get("class", [])
    assert "HTTP 503" in sections[1].get_text()
