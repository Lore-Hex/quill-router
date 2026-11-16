from __future__ import annotations

from fastapi.testclient import TestClient


def test_deidentification_docs_publish_opt_in_and_limits(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert 'id="deidentification"' in response.text
    assert 'extra_body={"deidentify": True}' in response.text
    assert "restores the original values" in response.text
    assert "no model call, prompt copy, or privacy worker" in response.text
    assert "never falls back to raw input" in response.text
    assert "256 KiB" in response.text
    assert "not guaranteed anonymization" in response.text
    assert "Only exact placeholders can be restored" in response.text
    assert "https://huggingface.co/nationaldesignstudio/rampart" in response.text
    assert "CC BY 4.0" in response.text
