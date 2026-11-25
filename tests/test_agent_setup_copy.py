from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.routes.inference import _require_messages_model


def test_claude_code_uses_anthropic_base_url_and_auth_token(client: TestClient) -> None:
    response = client.get("/docs/agent-setup")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    row = next(row for row in soup.select("tr") if row.find("td", string="Claude Code"))
    cells = row.find_all("td")
    assert cells[1].get_text(strip=True) == "https://api.trustedrouter.com"
    assert "ANTHROPIC_BASE_URL" in cells[2].get_text()
    assert "ANTHROPIC_AUTH_TOKEN" in cells[2].get_text()
    label = soup.find("p", string="Claude Code:")
    assert label is not None
    block = label.find_next_sibling("pre")
    assert block is not None
    assert block.get_text() == (
        'export ANTHROPIC_BASE_URL="https://api.trustedrouter.com"\n'
        'export ANTHROPIC_AUTH_TOKEN="$TRUSTEDROUTER_API_KEY"'
    )
    sdk_label = soup.find("p", string="Anthropic SDKs:")
    assert sdk_label is not None
    sdk_block = sdk_label.find_next_sibling("pre")
    assert sdk_block is not None
    assert 'export ANTHROPIC_API_KEY="$TRUSTEDROUTER_API_KEY"' in sdk_block.get_text()
    assert 'export ANTHROPIC_BASE_URL="https://api.trustedrouter.com"' in sdk_block.get_text()
    assert soup.find("a", href="https://code.claude.com/docs/en/llm-gateway-connect")

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    openai_setup = next(p for p in paragraphs if p.startswith("For Codex, Cursor"))
    assert "https://api.trustedrouter.com/v1" in openai_setup
    assert "Claude Code" not in openai_setup
    claude_setup = next(p for p in paragraphs if p.startswith("For Claude Code"))
    assert "Anthropic-compatible base URL https://api.trustedrouter.com without /v1" in claude_setup
    for element in soup.select("p, tr, pre"):
        text = element.get_text(" ", strip=True)
        if "Claude Code" in text:
            assert "OPENAI_BASE_URL" not in text
            assert "https://api.trustedrouter.com/v1" not in text


def test_messages_model_limit_matches_catalog_and_endpoint(client: TestClient) -> None:
    response = client.get("/docs/agent-setup")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    limit = soup.find(id="messages-model-limit")
    assert limit is not None
    supported = {model.id for model in MODELS.values() if model.supports_messages}
    published = {model.id for model in MODELS.values() if model.provider == "anthropic"}
    aliases = sorted(supported - published)
    assert supported
    assert supported == published | set(aliases)
    alias_copy = (
        "plus the catalog-marked aliases " + ", ".join(aliases)
        if aliases else "no TrustedRouter routing aliases are currently marked"
    )
    assert limit.get_text(" ", strip=True) == (
        "The Anthropic Messages endpoint accepts only models the catalog marks as "
        f"supporting Messages: Anthropic-published models ({alias_copy}); "
        "use the OpenAI-compatible endpoints for every other model."
    )
    assert limit.find_parent(class_="panel").find("h2").get_text() == "Model aliases"
    for model in MODELS.values():
        if model.supports_messages:
            assert _require_messages_model({"model": model.id}) is model
        else:
            with pytest.raises(HTTPException) as error:
                _require_messages_model({"model": model.id})
            assert error.value.status_code == 400


def test_docs_and_llms_keep_claude_code_separate_from_openai_setup(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    soup = BeautifulSoup(docs.text, "html.parser")
    card = soup.select_one('a.marketing-card[href="/docs/agent-setup"]')
    assert card is not None
    assert "use the Anthropic-compatible base URL for Claude Code and Anthropic SDKs" in card.get_text()
    for path in ("/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
        response = client.get(path)
        assert response.status_code == 200
        for sentence in re.split(r"[\n.!?]", response.text):
            if "Claude Code" in sentence:
                assert "OpenAI SDK" not in sentence
                assert "OPENAI_BASE_URL" not in sentence
