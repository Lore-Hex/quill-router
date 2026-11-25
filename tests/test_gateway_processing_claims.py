"""Public processing figures retain their measurement scope and shared source."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router import gateway_processing
from trusted_router.dashboard import PUBLIC_PAGES

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/trusted_router/templates"
PARTIAL = "public/_gateway_processing.html"
PAGES = tuple(
    f"/{slug}" for slug, page in PUBLIC_PAGES.items()
    if PARTIAL in (TEMPLATES / page.template).read_text()
)
DOCUMENTS = ("/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt")
MILLISECONDS = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?)\b", re.I)
LATENCY_WORDS = re.compile(r"\b(?:gateway|overhead|hop|latency)\b", re.I)
SCOPE = (
    "counting only time inside the gateway on the request itself and excluding "
    "account authorization and billing settlement calls and network time "
    "between the client, gateway, and provider"
)


def _unexpected_figures(text: str) -> list[str]:
    remaining = text.replace(gateway_processing.gateway_processing_claim(), "")
    return [
        match.group() for match in MILLISECONDS.finditer(remaining)
        if LATENCY_WORDS.search(remaining[max(0, match.start() - 100):match.end() + 100])
    ]


@pytest.mark.parametrize("path", (*PAGES, *DOCUMENTS))
@pytest.mark.parametrize("remeasured", [False, True])
def test_processing_copy_uses_shared_measurement(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, remeasured: bool,
) -> None:
    if remeasured:
        monkeypatch.setattr(gateway_processing, "GATEWAY_PROCESSING_MEDIAN_MS", 4)
        monkeypatch.setattr(gateway_processing, "MEASUREMENT_DATE", "November 25, 2026")
        monkeypatch.setattr(gateway_processing, "PROVIDER_FIRST_BYTE_SECONDS", 0.8)
    response = client.get(path)
    assert response.status_code == 200
    claim = gateway_processing.gateway_processing_claim()
    if path in DOCUMENTS:
        paragraphs = response.text.splitlines()
    else:
        soup = BeautifulSoup(response.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        for paragraph in soup.find_all("p"):
            if claim in paragraph.get_text(" ", strip=True):
                assert paragraph.find("a", href="/status")
    matches = [p for p in paragraphs if MILLISECONDS.search(p)]
    assert matches
    for paragraph in matches:
        assert claim in paragraph
        assert SCOPE in paragraph
        assert gateway_processing.MEASUREMENT_DATE in paragraph
        assert "successful chat and responses requests across all four gateway regions" in paragraph
        if path in DOCUMENTS:
            assert "](https://trustedrouter.com/status)" in paragraph
    assert not _unexpected_figures(" ".join(paragraphs))


def test_required_pages_use_shared_partial() -> None:
    assert {
        "/docs/simple-start", "/llm-gateway-pricing", "/llm-provider-latency-benchmarks",
    } <= set(PAGES)


def test_other_gateways_statement_is_structural(client: TestClient) -> None:
    claim = gateway_processing.GATEWAY_HOP_CLAIM
    assert "one network hop between the client and the provider" in claim
    assert "OpenRouter is the same extra hop" in claim
    assert not re.search(r"\d|milliseconds?|seconds?|latency|faster|slower", claim, re.I)
    for path in (*DOCUMENTS, "/docs/simple-start", "/llm-provider-latency-benchmarks"):
        text = BeautifulSoup(client.get(path).text, "html.parser").get_text(" ", strip=True)
        assert claim in text


def test_public_templates_have_no_other_gateway_millisecond_claims() -> None:
    failures = {}
    for path in sorted((TEMPLATES / "public").rglob("*.html")):
        if path.stem.startswith("status"):
            continue
        soup = BeautifulSoup(path.read_text(), "html.parser")
        for node in soup.select("script, style"):
            node.decompose()
        for block in soup.find_all(["p", "li", "tr", "pre", "h1", "h2", "h3"]):
            text = block.get_text(" ", strip=True)
            if figures := _unexpected_figures(text):
                failures.setdefault(str(path.relative_to(TEMPLATES)), []).extend(figures)
    assert not failures, failures


def test_public_registry_and_llms_sources_have_no_other_gateway_figures() -> None:
    tree = ast.parse((ROOT / "src/trusted_router/dashboard.py").read_text())
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and "llms" in node.name)
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.target.id == "PUBLIC_PAGES")
    ]
    assert nodes
    for node in nodes:
        for value in ast.walk(node):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                assert not _unexpected_figures(value.value), (value.lineno, value.value)


@pytest.mark.parametrize("word", ["gateway", "overhead", "hop", "latency"])
@pytest.mark.parametrize("unit", ["ms", "milliseconds"])
def test_numeric_rule_allows_only_complete_shared_claim(word: str, unit: str) -> None:
    claim = gateway_processing.gateway_processing_claim()
    assert not _unexpected_figures(claim)
    allowed = {match.group() for match in MILLISECONDS.finditer(claim)}
    assert allowed == {f"{gateway_processing.GATEWAY_PROCESSING_MEDIAN_MS} milliseconds"}
    for number in (gateway_processing.GATEWAY_PROCESSING_MEDIAN_MS,
                   gateway_processing.GATEWAY_PROCESSING_MEDIAN_MS + 1):
        assert _unexpected_figures(f"The {word} takes {number} {unit}.")
        assert _unexpected_figures(f"{number} {unit} of {word}.")
    assert _unexpected_figures(claim.replace(SCOPE, "including network time"))
    assert _unexpected_figures(claim.replace(gateway_processing.MEASUREMENT_DATE, "today"))
