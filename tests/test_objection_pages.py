"""Public answers stay discoverable and their examples use the routing contract."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.pinned_manifests import serve_committed_route
from tests.test_catalog_count_claims import invalid_claims
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.routes.inference import _validate_chat_messages
from trusted_router.routing import (
    _PROVIDER_ROUTING_FIELDS,
    chat_route_endpoint_candidates,
    provider_route_preferences,
)
from trusted_router.schemas import GatewayAuthorizeRequest

PAGES = ("/who-can-see-your-data", "/docs/simple-start")
LLMS_DOCUMENTS = ("/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt")
RERANK_SCOPE = (
    "reranking is available through post /v1/rerank with voyage rerank models, prepaid, on the "
    "gcp api hosts; it is not yet served on the aws or azure api hosts."
)


@pytest.mark.parametrize("path", PAGES)
def test_objection_page_renders_and_is_discoverable(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.find("link", rel="canonical")["href"] == f"https://trustedrouter.com{path}"
    assert len(soup.find_all("h1")) == 1
    assert not invalid_claims(response.text)
    assert "\u2014" not in soup.select_one(".content-document").get_text()
    assert f"<loc>https://trustedrouter.com{path}</loc>" in client.get("/sitemap-core.xml").text
    for document in LLMS_DOCUMENTS:
        text = client.get(document).text
        assert path in text
        assert "## Common objections, answered" in text
        for letter in "ABCDEFG":
            assert f"- {letter}. " in text
        assert not invalid_claims(text)
    assert f'href="{path}"' in client.get("/").text


def _curl_body(code: str) -> dict[str, Any]:
    match = re.search(r"-d '(.*?)'", code, re.S)
    assert match is not None
    return json.loads(match.group(1))


def _python_body(code: str) -> dict[str, Any]:
    tree = ast.parse(code)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "create"]
    assert len(calls) == 1
    kwargs = {keyword.arg: ast.literal_eval(keyword.value) for keyword in calls[0].keywords}
    return {**kwargs.pop("extra_body"), **kwargs}


@pytest.mark.parametrize("path", PAGES)
def test_documented_requests_use_real_schema_and_pin_one_candidate(
    client: TestClient, test_settings: Settings, path: str,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    soup = BeautifulSoup(client.get(path).text, "html.parser")
    documented_controls = set(re.findall(
        r"provider\.([a-z_]+)", soup.select_one(".content-document").get_text(),
    ))
    assert documented_controls <= _PROVIDER_ROUTING_FIELDS
    examples = soup.select("pre[data-example]")
    assert len(examples) == (2 if path == "/docs/simple-start" else 1)
    bodies = []
    for example in examples:
        code = example.get_text()
        body = (_python_body(code) if example["data-example"] == "python"
                else _curl_body(code))
        bodies.append(body)
        # The authorization schema intentionally excludes prompt bodies.
        # Validate those with the actual inference validator, and all other
        # top-level fields against the production authorization model.
        _validate_chat_messages(body)
        metadata = {key: value for key, value in body.items() if key != "messages"}
        assert metadata.keys() <= GatewayAuthorizeRequest.model_fields.keys()
        GatewayAuthorizeRequest.model_validate({**metadata, "api_key_hash": "test-key-hash"})
        # This parser rejects unknown provider fields and invalid values.
        prefs = provider_route_preferences(body)
        assert prefs.allow_fallbacks is False
        assert len(prefs.only) == 1
        # The documented pair is the maker's own route; it is the vehicle
        # here whether or not the hourly refresh has it down this hour.
        (only,) = prefs.only
        serve_committed_route(monkeypatch, tmp_path, f"{body['model']}@{only}/prepaid")
        candidates = chat_route_endpoint_candidates(body, test_settings)
        assert len(candidates) == 1
        model, endpoint = candidates[0]
        assert model.id == body["model"]
        assert endpoint.provider in prefs.only
    if len(bodies) == 2:
        assert bodies[0] == bodies[1]


def test_data_page_preserves_privacy_limits(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[0]).text, "html.parser")
    table = soup.select_one(".content-reference table")
    assert len(table.select("tbody tr")) == 4
    text = soup.get_text(" ", strip=True)
    for claim in (
        "processes plaintext", "protected hashes", "BYOK credentials encrypted",
        "does not establish upstream retention", "aggregated usage data", "Broadcast",
        "30 days", "26 hours", "6 hours", "project's IAM administration",
        "not an upstream data-residency guarantee", "certificate fingerprint",
        "confidentialcomputing.googleapis.com", "quill-cloud",
    ):
        assert claim in text


def test_data_page_leads_with_verifiable_gateway_facts(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[0]).text, "html.parser")
    document = soup.select_one(".content-document")
    for paragraph in (soup.select_one("main p"), document.find("p")):
        text = paragraph.get_text(" ", strip=True)
        for claim in (
            "TLS terminates inside an attested enclave", "running published source",
            "control plane never receives", "no prompt or output logs",
            "Anyone can verify the running image against the published release",
        ):
            assert claim in text
        assert "upstream" not in text.lower()
    text = soup.select_one("main").get_text(" ", strip=True).lower()
    assert text.index("attested enclave") < text.index("upstream provider")


@pytest.mark.parametrize("path", (*PAGES, *LLMS_DOCUMENTS))
def test_objection_pages_and_agent_text_scope_reranking(
    client: TestClient, path: str,
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    text = (response.text if path in LLMS_DOCUMENTS else
            BeautifulSoup(response.text, "html.parser").select_one(".content-document")
            .get_text(" ", strip=True)).lower()
    if path != PAGES[0]:
        for claim in ("post /v1/rerank", "voyage", "prepaid", "gcp api hosts",
                      "not yet served on the aws or azure api hosts"):
            assert claim in text
    assert "no reranking endpoint today" not in text
    if path in (*PAGES, "/llms.txt", "/docs/llms.txt"):
        # Permit the one scoped sentence, but no other reranking claim.
        assert "rerank" not in " ".join(text.split()).replace(RERANK_SCOPE, "")


def test_simple_start_scopes_capabilities_and_business_claims(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[1]).text, "html.parser")
    text = soup.select_one(".content-document").get_text(" ", strip=True)
    for claim in (
        "Embeddings", "Transcription", "POST /v1/rerank", "Voyage", "prepaid", "GCP API hosts", "Batch", "Video",
        "does not offer fine-tuning", "does not run inference GPUs",
        "SOC 2 is in its observation window", "SLA, MSA and DPA",
        "preferences, not guarantees or timeouts", "independent of OpenRouter",
        "network hop and a service dependency", "Robot, Robot & Human",
    ):
        assert claim in text
    assert text.count("5.5%") == 1
    assert soup.find("a", href="https://nytokenexchange.com/buy")
    assert soup.find("a", href="/docs/spend-controls")
    assert MODELS["openai/text-embedding-3-small"].supports_embeddings
    assert any(endpoint.provider == "tinfoil"
               for endpoint in endpoints_for_model("openai/whisper-large-v3-turbo"))
    assert 'href="/docs/simple-start"' in client.get("/docs").text
    assert 'href="/who-can-see-your-data"' in client.get("/security").text


def test_vercel_comparison_no_longer_denies_supported_modalities(client: TestClient) -> None:
    text = client.get("/compare/vercel-ai-gateway").text
    assert "TrustedRouter equivalent and would stay on Vercel" not in text
    assert "We do not match Vercel's realtime voice, speech, transcription, or reranking" not in text
    assert "TrustedRouter supports speech and transcription" in text
    assert "realtime voice sessions do not carry over" in text
    assert "Reranking is available through POST /v1/rerank with Voyage rerank models, prepaid, on the GCP API hosts" in text
    assert "not yet served on the AWS or Azure API hosts" in text
    assert "TrustedRouter supports speech, transcription, and reranking" not in text
