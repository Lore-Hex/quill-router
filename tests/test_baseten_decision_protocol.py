"""System One models must never be sent to the chat-completions adapter."""
import json

import pytest

from scripts.pricing.providers import baseten
from trusted_router.provider_contracts import provider_model_operator_held

DECISIONS = (
    "decision-models/baseten-elephant-9b-latest",
    "decision-models/baseten-mammoth-27b-latest",
    "decision-models/baseten-joey-2b-latest",
)


@pytest.mark.parametrize("native", (*DECISIONS, "decision-models/future-model"))
def test_systemone_cannot_be_authorized_as_chat_even_from_stale_manifest(native):
    assert provider_model_operator_held("baseten", native)
    assert provider_model_operator_held("baseten", "alternate/canonical-name", native)
    assert not provider_model_operator_held("another-provider", native)
    assert not provider_model_operator_held("baseten", "moonshotai/kimi-k3")


def test_discovery_keeps_decision_metadata_without_publishing_chat(monkeypatch, tmp_path, httpx_mock):
    monkeypatch.setattr(baseten, "EXPECTED_MODELS", [])
    monkeypatch.setattr(baseten, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(baseten, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(baseten, "MANIFEST_PATH", tmp_path / "baseten.json")
    rows = [{"id": model, "pricing": {"prompt": "0.0000001", "completion": "0"}}
            for model in DECISIONS]
    rows.append({"id": "moonshotai/Kimi-K3", "pricing": {"prompt": "0.000001", "completion": "0.000003"}})
    httpx_mock.add_response(url=baseten.URL, json={"data": rows}, is_reusable=True)
    for _ in range(2):
        result = baseten.fetch()
        baseten.write_provider_manifest(result)
        saved = {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}
        for model in DECISIONS:
            row = saved[model]
            assert row["model_type"] == "decision"
            assert row["endpoints"] == ["decide"]
            assert row["routable"] is False
            assert row["routable_reason"] == "systemone-adapter-not-implemented"
            assert row["input_token_price_per_m"] == 100_000
        assert saved["moonshotai/kimi-k3"].get("routable", True) is True


def test_committed_catalog_has_no_baseten_systemone_chat_routes():
    from trusted_router.catalog import MODEL_ENDPOINTS

    assert not [endpoint for endpoint in MODEL_ENDPOINTS.values()
                if endpoint.provider == "baseten" and endpoint.upstream_id.startswith("decision-models/")]
    rows = {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}
    for model in DECISIONS:
        assert rows[model]["model_type"] == "decision"
        assert rows[model]["routable"] is False
