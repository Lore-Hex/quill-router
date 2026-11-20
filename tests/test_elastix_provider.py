import json
from copy import deepcopy

import pytest

from scripts.pricing.providers import elastix
from trusted_router.provider_contract import PROVIDER_CATALOG_V2_EXAMPLE


def catalog():
    doc = deepcopy(PROVIDER_CATALOG_V2_EXAMPLE)
    doc["provider"]["id"] = elastix.SLUG
    return doc


def test_unpriced_openai_listing_is_not_a_price_source():
    rows = [{"id": name, "object": "model", "created": 0, "owned_by": "elastix"}
            for name in ("gpt-oss-120b", "qwen3.8-27b")]
    with pytest.raises(RuntimeError, match="priced Provider Reliability Contract v2"):
        elastix.discover({"object": "list", "data": rows})


def test_wrong_identity_and_invalid_price_rejected():
    with pytest.raises(RuntimeError, match="provider identity"):
        elastix.discover(PROVIDER_CATALOG_V2_EXAMPLE)
    doc = catalog()
    doc["data"][0]["capabilities"]["prompt_caching"] = True
    doc["data"][0]["pricing"]["cached_input"] = "0.51"
    with pytest.raises(RuntimeError, match="cached input rate"):
        elastix.discover(doc)


def test_valid_catalog_is_held_and_failed_fetch_cannot_republish(monkeypatch, tmp_path):
    doc = catalog()
    doc["data"][0]["capabilities"]["prompt_caching"] = True
    doc["data"][0]["pricing"]["cached_input"] = "0.10"
    path = tmp_path / "elastix.json"
    monkeypatch.setattr(elastix, "MANIFEST_PATH", path)
    monkeypatch.setattr(elastix, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(elastix, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("ELASTIX_API_KEY", "test-only")

    def fetch(url, *, extra_headers, follow_redirects):
        assert url == "https://api.elastix.ai/v1/models" and follow_redirects is False
        assert extra_headers == {"Authorization": "Bearer test-only"}
        return doc

    monkeypatch.setattr(elastix, "fetch_json", fetch)
    result = elastix.fetch()
    assert not result.include_in_price_index
    price = result.prices["acme/atlas-70b"].tiers[0]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m, price.prompt_cached_micro_per_m) == (
        500_000, 1_500_000, 100_000,
    )
    elastix.write_provider_manifest(result)
    row, = json.loads(path.read_text())["models"]
    assert row["routable"] is False and row["routable_reason"] == "marketplace-onboarding-pending"
    assert row["upstream_id"] == "acme/atlas-70b"
    assert "prompt_caching" in row["supported_features"]
    assert "mailto:" not in path.read_text()
    monkeypatch.delenv("ELASTIX_API_KEY")
    with pytest.raises(RuntimeError, match="ELASTIX_API_KEY"):
        elastix.fetch()
    with pytest.raises(RuntimeError, match="successful discovery"):
        elastix.write_provider_manifest(result)
    assert not elastix.UPSTREAM_ID_MAP


def test_staging_does_not_register_customer_routes_or_schedule_price_refresh():
    from scripts.pricing.refresh import PROVIDER_SLUGS
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS

    assert elastix.SLUG not in PROVIDER_SLUGS
    assert elastix.SLUG not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert not any(ep.provider == elastix.SLUG for ep in MODEL_ENDPOINTS.values())
