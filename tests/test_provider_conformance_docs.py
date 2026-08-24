from __future__ import annotations

from fastapi.testclient import TestClient


def test_provider_conformance_docs_publish_gateway_contract(client: TestClient) -> None:
    response = client.get("/docs/provider-conformance")

    assert response.status_code == 200
    assert (
        "uvx trustedrouter-provider-check --base-url "
        "https://your-endpoint.example/v1 --model your-model" in response.text
    )
    assert "stream=true" in response.text
    assert "[DONE]" in response.text
    assert "include_usage" in response.text
    assert "Tool-call deltas missing" in response.text
    assert "Mid-stream error after HTTP" in response.text
    assert "Buffered streaming" in response.text
    assert "advertised model returns" in response.text
    assert "vLLM" in response.text
    assert "TensorRT-LLM" in response.text
    assert "fail" in response.text
    assert "warn" in response.text
    assert "parity gate" in response.text
    assert "Apache-2.0" in response.text
    assert "https://github.com/Lore-Hex/trustedrouter-provider-check" in response.text


def test_provider_conformance_docs_are_discoverable(client: TestClient) -> None:
    path = "/docs/provider-conformance"

    assert f'href="{path}"' in client.get("/docs").text
    assert path in client.get("/llms.txt").text
    assert path in client.get("/docs/llms.txt").text
    assert path in client.get("/docs/llms-full.txt").text
    assert f"<loc>https://trustedrouter.com{path}</loc>" in client.get("/sitemap-core.xml").text
