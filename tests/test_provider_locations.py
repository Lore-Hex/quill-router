from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trusted_router import provider_locations
from trusted_router.catalog import PROVIDERS
from trusted_router.config import Settings
from trusted_router.dashboard import _provider_detail_view, public_provider_detail_html
from trusted_router.provider_locations import (
    PROVIDER_INFERENCE_LOCATIONS,
    InferenceLocations,
    provider_inference_locations,
    provider_model_locations,
    telnyx_model_locations,
)


def test_reviewed_claims_have_dated_sources_and_no_routing_privileges() -> None:
    for slug, claim in PROVIDER_INFERENCE_LOCATIONS.items():
        assert slug in PROVIDERS
        assert claim.reviewed_on and date.fromisoformat(claim.reviewed_on)
        assert claim.sources
        assert all(url.startswith("https://") for _, url in claim.sources)
        assert claim.routing and claim.provider_pinning and claim.trustedrouter_pinning
        assert claim.declaration and claim.evidence
    assert not provider_inference_locations("engy").locations
    assert not provider_inference_locations("novita").locations
    assert not provider_inference_locations("siliconflow").locations


def test_unreviewed_provider_stays_unknown_despite_jurisdiction_or_privacy() -> None:
    assert provider_inference_locations("openai") == InferenceLocations()
    assert provider_inference_locations("../private") == InferenceLocations()
    assert not provider_model_locations("../private").model_regions
    assert not provider_model_locations("deepinfra").model_regions


def _snapshot(regions: object) -> dict:
    return {"provider": "telnyx", "generated_at": "2026-09-26T12:00:00Z", "models": [
        {"id": "z-ai/glm-5.3-flash", "provider_regions": regions},
    ]}


def test_catalog_regions_are_availability_not_storage_country_inferences() -> None:
    snapshot = telnyx_model_locations(_snapshot(["USA", "EU", "USA", "AUS", "UAE"]))
    assert snapshot.generated_at == "2026-09-26T12:00:00Z"
    assert snapshot.model_regions["z-ai/glm-5.3-flash"] == (
        "United States", "European Union (country unspecified)", "Australia", "United Arab Emirates",
    )
    assert "Germany" not in str(snapshot.model_regions)


@pytest.mark.parametrize("regions", [None, [], "USA", ["USA", "NEW"], ["USA", 9]])
def test_unknown_catalog_codes_never_silently_narrow_geography(regions: object) -> None:
    assert not telnyx_model_locations(_snapshot(regions)).model_regions


@pytest.mark.parametrize("payload", [None, [], {}, {"provider": "other"},
    {"provider": "telnyx", "models": [{"id": "a", "provider_regions": ["USA"]}]}])
def test_missing_or_undated_catalog_is_unknown(payload: object) -> None:
    assert not telnyx_model_locations(payload).model_regions


@pytest.mark.parametrize("timestamp", ["yesterday", "2026-09-26", "2026-09-26T12:00:00"])
def test_invalid_or_timezone_free_snapshot_is_unknown(timestamp: str) -> None:
    payload = _snapshot(["USA"])
    payload["generated_at"] = timestamp
    assert not telnyx_model_locations(payload).model_regions


def test_duplicate_model_rows_cannot_hide_a_location() -> None:
    payload = _snapshot(["USA", "EU"])
    payload["models"].append({"id": "z-ai/glm-5.3-flash", "provider_regions": ["USA"]})
    assert not telnyx_model_locations(payload).model_regions


def test_location_snapshot_uses_existing_manifest_and_survives_bad_file(monkeypatch) -> None:
    provider_locations._telnyx_location_snapshot.cache_clear()
    manifest = Path(provider_locations.__file__).parent / "data/provider_models/telnyx.json"
    expected = telnyx_model_locations(json.loads(manifest.read_text()))
    assert provider_model_locations("telnyx") == expected
    provider_locations._telnyx_location_snapshot.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", lambda *args, **kwargs: "broken json")
        assert not provider_model_locations("telnyx").model_regions
    provider_locations._telnyx_location_snapshot.cache_clear()


def test_only_currently_served_models_get_location_rows(monkeypatch) -> None:
    monkeypatch.setattr(provider_locations, "_telnyx_location_snapshot", lambda: telnyx_model_locations(
        {"provider": "telnyx", "generated_at": "2026-09-26T12:00:00Z", "models": [
            {"id": "qwen/qwen3.8-27b", "provider_regions": ["USA"]},
            {"id": "retired/model", "provider_regions": ["EU"]},
        ]},
    ))
    view = _provider_detail_view(PROVIDERS["telnyx"], served_models=[{"id": "qwen/qwen3.8-27b"}])
    assert view["model_locations"] == [{"id": "qwen/qwen3.8-27b", "regions": ("United States",)}]


@pytest.mark.parametrize("slug", sorted(PROVIDER_INFERENCE_LOCATIONS))
def test_provider_pages_expose_location_evidence_and_limits(test_settings: Settings, slug: str) -> None:
    html = public_provider_detail_html(test_settings, slug)
    assert html is not None
    for text in ("Inference locations", "Can locations change?", "Provider region pinning",
                 "Pinning through TrustedRouter", "Infrastructure declaration", "2026-09-26",
                 "company jurisdiction, not GPU location", "not by themselves restrict geography"):
        assert text in html
    for _, url in PROVIDER_INFERENCE_LOCATIONS[slug].sources:
        assert url in html


def test_telnyx_and_pearl_do_not_overstate_guarantees(test_settings: Settings) -> None:
    html = public_provider_detail_html(test_settings, "telnyx")
    assert "not per-request receipts or enforced residency guarantees" in html
    assert "Catalog snapshot:" in html
    assert "not establish Germany as the inference country" in html
    assert "without Telnyx&#39;s strict region controls" in html
    pearl = public_provider_detail_html(test_settings, "pearl")
    assert "provider-wide information" in pearl and "Taiwan" in pearl
    assert "marketplace application" in pearl and "exhaustive failover scope unconfirmed" in pearl


def test_unknown_and_gateway_pages_do_not_invent_locations(test_settings: Settings) -> None:
    html = public_provider_detail_html(test_settings, "openai")
    assert "No inference-location declaration has been verified" in html
    assert "Not yet reviewed" in html
    assert 'id="inference-locations"' not in public_provider_detail_html(test_settings, "trustedrouter")


def test_location_evidence_is_html_escaped(test_settings: Settings, monkeypatch) -> None:
    monkeypatch.setitem(PROVIDER_INFERENCE_LOCATIONS, "engy", replace(
        PROVIDER_INFERENCE_LOCATIONS["engy"], scope="<script>alert(1)</script>",
    ))
    html = public_provider_detail_html(test_settings, "engy")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
