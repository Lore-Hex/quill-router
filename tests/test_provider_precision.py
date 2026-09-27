from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from trusted_router import provider_precision
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, model_to_openrouter_shape
from trusted_router.config import Settings
from trusted_router.dashboard import public_model_detail_html
from trusted_router.provider_precision import (
    ProviderPrecision,
    endpoint_precision,
    endpoint_precision_metadata,
    endpoint_quantization,
)


def _endpoint(provider: str, model_id: str):
    return next(e for e in MODEL_ENDPOINTS.values()
                if e.provider == provider and e.model_id == model_id and e.usage_type == "Credits")


def _records():
    return [ProviderPrecision.from_dict(row)
            for row in json.loads(provider_precision._SNAPSHOT.read_text())]


@pytest.mark.parametrize("record", _records(), ids=lambda r: f"{r.provider}/{r.model_id}")
def test_reviewed_precision_exactly_matches_catalog_and_has_pinned_sources(record) -> None:
    endpoint = _endpoint(record.provider, record.model_id)
    assert endpoint.upstream_id == record.upstream_id
    assert endpoint_precision(endpoint) == record
    assert record.reviewed_on == date(2026, 9, 27)
    assert record.runtime_verified is False
    assert record.evidence_type == "published_serving_config"
    assert record.quantization in record.weight_formats
    assert record.notes
    assert any(record.model_revision in source.url and "/config.json" in source.url
               for source in record.sources)
    assert any("github.com" in source.url or "api.chutes.ai/chutes/code/" in source.url
               for source in record.sources)
    if record.provider == "chutes":
        assert record.sources[0].sha256


def test_provider_and_weight_precision_are_not_inferred_from_model_name_or_tee() -> None:
    endpoint = _endpoint("tinfoil", "z-ai/glm-5.3-flash")
    for changed in (replace(endpoint, provider="phala"),
                    replace(endpoint, model_id="z-ai/unreviewed"),
                    replace(endpoint, upstream_id="glm-5-3-flash-new"),
                    replace(endpoint, upstream_id=None)):
        assert endpoint_precision(changed) is None
        assert endpoint_precision_metadata(changed) is None
        assert endpoint_quantization(changed) is None


def test_same_model_can_have_different_weights_and_cache_formats() -> None:
    tinfoil = endpoint_precision_metadata(_endpoint("tinfoil", "z-ai/glm-5.3-flash"))
    private = endpoint_precision_metadata(_endpoint("privatemode", "z-ai/glm-5.3-flash"))
    near = endpoint_precision_metadata(_endpoint("near-ai", "z-ai/glm-5.3-flash"))
    assert tinfoil["quantization"] == "nvfp4"
    assert tinfoil["weight_formats"] == ["nvfp4", "fp8"]
    assert private["quantization"] == near["quantization"] == "fp8"
    assert near["kv_cache_dtype"] == "bfloat16"
    assert private["kv_cache_dtype"] == "fp8"
    assert len({r["model_revision"] for r in (tinfoil, private, near)}) == 3


def test_mixed_experts_and_nested_text_quantization_are_preserved() -> None:
    ds = endpoint_precision_metadata(_endpoint("tinfoil", "deepseek/deepseek-v4.1-flash"))
    assert ds["label"] == "FP8 + FP4 experts"
    assert ds["weight_formats"] == ["fp8", "fp4"]
    assert ds["kv_cache_dtype"] is None
    kimi = endpoint_precision_metadata(_endpoint("chutes", "moonshotai/kimi-k2.6"))
    assert kimi["quantization"] == "int4"
    assert "excluded" in kimi["notes"]
    k3 = endpoint_precision_metadata(_endpoint("tinfoil", "moonshotai/kimi-k3"))
    assert k3["quantization"] == "mxfp4"
    assert k3["kv_cache_dtype"] == "fp8"


def test_metadata_is_a_copy_and_lookup_has_no_io_after_first_load(monkeypatch) -> None:
    provider_precision._precision_index.cache_clear()
    endpoint = _endpoint("tinfoil", "openai/gpt-oss-120b")
    first = endpoint_precision_metadata(endpoint)
    first["sources"][0]["url"] = "changed"
    first["weight_formats"].append("fp32")
    with monkeypatch.context() as patch:
        def fail_read(*args, **kwargs):
            raise AssertionError("Unexpected repeat file read")
        patch.setattr(type(provider_precision._SNAPSHOT), "read_text", fail_read)
        second = endpoint_precision_metadata(endpoint)
    assert second["sources"][0]["url"] != "changed"
    assert second["weight_formats"] == ["mxfp4"]


@pytest.mark.parametrize("payload", ["broken JSON", "null", "{}", "[{}]", "[]"])
def test_invalid_or_empty_snapshot_cannot_take_down_catalog(tmp_path, monkeypatch, payload) -> None:
    path = tmp_path / "precision.json"
    path.write_text(payload)
    provider_precision._precision_index.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(provider_precision, "_SNAPSHOT", path)
            assert endpoint_precision(_endpoint("tinfoil", "z-ai/glm-5.3")) is None
    finally:
        provider_precision._precision_index.cache_clear()


def test_duplicate_routes_fail_closed(tmp_path, monkeypatch) -> None:
    row = json.loads(provider_precision._SNAPSHOT.read_text())[0]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps([row, row]))
    provider_precision._precision_index.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(provider_precision, "_SNAPSHOT", path)
            assert provider_precision._precision_index() == {}
    finally:
        provider_precision._precision_index.cache_clear()


@pytest.mark.parametrize("change", [
    {"weight_formats": "fp8"}, {"weight_formats": []}, {"quantization": "unknown"},
    {"runtime_verified": True}, {"model_revision": "main"},
    {"sources": [{"title": "Source", "url": "http://example.com"}]},
    {"evidence_type": "attested"},
])
def test_invalid_evidence_cannot_be_published(change) -> None:
    row = json.loads(provider_precision._SNAPSHOT.read_text())[0]
    with pytest.raises((TypeError, ValueError)):
        ProviderPrecision.from_dict({**row, **change})


def test_missing_snapshot_fails_closed(tmp_path, monkeypatch) -> None:
    provider_precision._precision_index.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(provider_precision, "_SNAPSHOT", tmp_path / "missing.json")
            assert provider_precision._precision_index() == {}
    finally:
        provider_precision._precision_index.cache_clear()


def test_catalog_and_endpoint_api_expose_same_reviewed_metadata(client) -> None:
    endpoint = _endpoint("tinfoil", "z-ai/glm-5.3-flash")
    model = model_to_openrouter_shape(MODELS[endpoint.model_id])
    catalog_row = next(e for e in model["trustedrouter"]["endpoints"] if e["id"] == endpoint.id)
    response = client.get(f"/v1/models/{endpoint.model_id}/endpoints")
    assert response.status_code == 200
    row = next(e for e in response.json()["data"] if e["endpoint_id"] == endpoint.id)
    assert row["quantization"] == catalog_row["quantization"] == "nvfp4"
    assert row["trustedrouter"]["precision"] == catalog_row["precision"]
    assert catalog_row["precision"]["runtime_verified"] is False
    unknown = next(e for e in model["trustedrouter"]["endpoints"] if e["provider"] == "zai")
    assert unknown["quantization"] is None
    assert unknown["precision"] is None


def test_model_page_exposes_reviewed_sources_without_claiming_runtime_proof(test_settings: Settings) -> None:
    html = public_model_detail_html(test_settings, "z-ai/glm-5.3-flash")
    assert html is not None
    for text in ("Weight format", "NVFP4 + FP8", "Unknown", "Pinned weight configuration",
                 "Per-request precision is not verified", "KV cache: bfloat16", "2026-09-27"):
        assert text in html
    assert "240131d6a447c8d89acd428c5ddfc85598651744" in html


def test_routing_documentation_distinguishes_metadata_from_filtering(client) -> None:
    response = client.get("/docs/provider-routing")
    assert response.status_code == 200
    assert "filtering by it is not implemented" in response.text
    assert "runtime_verified" in response.text
