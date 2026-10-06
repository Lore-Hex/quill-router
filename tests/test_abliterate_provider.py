from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import _direct_openai, abliterate


def docs_prices():
    return ",".join(
        f'{{name:"{name}",blurb:"Description",input_per_m:"${prompt}",output_per_m:"${output}"}}'
        for name, prompt, output in (
            ("abliterate-0.3-fast", "0.50", "1.00"),
            ("abliterate-0.3-balanced", "0.90", "2.00"),
            ("abliterate-0.3-clever", "2.50", "5.00"),
            ("abliterated-research-0.1", "2.50", "20.00"),
        )
    )


def test_prices_are_usd_per_million_not_per_token():
    prices = abliterate._parse_prices(docs_prices())
    assert len(prices) == 4
    assert prices["abliterate/abliterate-0.3-fast"].prompt_micro_per_m == 500_000
    assert prices["abliterate/abliterate-0.3-balanced"].completion_micro_per_m == 2_000_000
    assert prices["abliterate/abliterated-research-0.1"].completion_micro_per_m == 20_000_000


@pytest.mark.parametrize("value", ["$NaN", "$-1", "$0", "EUR0.50", "$Infinity", "$0.5000009"])
def test_malformed_prices_fail_closed(value):
    with pytest.raises(RuntimeError):
        abliterate._parse_prices(docs_prices().replace("$0.50", value))


def test_missing_or_duplicate_cards_fail_closed():
    for source in ("", docs_prices().split("},{", 1)[0], docs_prices() + docs_prices()):
        with pytest.raises(RuntimeError):
            abliterate._parse_prices(source)


@pytest.mark.parametrize("reference", [
    "https://abliteration.ai/assets/docs.js", "https://evil.example/assets/docs.js",
    "//evil.example/assets/docs.js", "http://abliterate.ai/assets/docs.js",
    "https://abliterate.ai/api/me", "/assets/docs.js?token=secret",
])
def test_docs_assets_are_same_origin_only(reference):
    with pytest.raises(RuntimeError):
        abliterate._asset_url(reference)


def test_load_prices_tracks_hashed_assets_without_executing_them(monkeypatch):
    sources = {
        abliterate.PRICING_URL: '<script type="module" src="/assets/index-new.js"></script>',
        "https://abliterate.ai/assets/index-new.js": 'import("./DocsView-new.js")',
        "https://abliterate.ai/assets/DocsView-new.js": docs_prices(),
    }
    monkeypatch.setattr(abliterate, "fetch_html", sources.__getitem__)
    assert len(abliterate._load_prices()) == 4
    sources["https://abliterate.ai/assets/index-new.js"] = "missing docs"
    with pytest.raises(RuntimeError, match="not found"):
        abliterate._load_prices()


def test_refresh_cannot_clear_accounting_hold_or_make_paid_probes(monkeypatch, tmp_path):
    adapter = _direct_openai.DirectOpenAIProvider(abliterate.CATALOG.spec, manifest_path=tmp_path / "abliterate.json")
    monkeypatch.setenv("ABLITERATE_API_KEY", "test-only-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: {
        "data": [{"id": name} for name in abliterate.EXPLICIT_MODEL_MAP]
    })
    monkeypatch.setattr(adapter, "_joined_prices", lambda: abliterate._parse_prices(docs_prices()))
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **_kw: pytest.fail("held models must not spend"))
    for _ in range(2):
        result = adapter.fetch()
        adapter.write_provider_manifest(result)
        manifest = json.loads(adapter.manifest_path.read_text())
        assert len(manifest["models"]) == 4
        for row in manifest["models"]:
            assert row["routable"] is False
            assert row["routable_reason"] == "upstream-usage-unavailable"
            assert row["upstream_id"] in abliterate.EXPLICIT_MODEL_MAP
            assert row.get("context_length") is None


def test_registration_privacy_secret_and_no_live_routes():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
    from trusted_router.sentry_config import SENSITIVE_STRING_FRAGMENTS
    from trusted_router.services.inference_errors import default_provider_secret_ref

    provider = PROVIDERS["abliterate"]
    assert provider.supports_prepaid
    assert not provider.provider_zero_data_retention
    assert not provider.provider_confidential_compute
    assert "abliterate" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "abliterate" in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert any(row[0] == "abliterate" for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert not any(endpoint.provider == "abliterate" for endpoint in MODEL_ENDPOINTS.values())
    assert OPENAI_COMPATIBLE_PROVIDERS["abliterate"] == (("ABLITERATE_API_KEY",), abliterate.BASE_URL)
    assert default_provider_secret_ref("abliterate") == "env://ABLITERATE_API_KEY"
    assert "abliterate_api_key" in SENSITIVE_STRING_FRAGMENTS
    root = Path(__file__).resolve().parents[1]
    assert "ABLITERATE_API_KEY:trustedrouter-abliterate-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()


def test_accounting_hold_is_valid_quarantine_not_valid_routing():
    from datetime import UTC, datetime, timedelta

    from trusted_router.provider_manifest_policy import (
        EXPIRED_PROVIDER_MANIFEST,
        provider_manifest_canary_quarantine_valid_until,
        provider_manifest_valid_until,
    )

    raw = json.loads(abliterate.MANIFEST_PATH.read_text())
    generated = datetime.now(UTC)
    raw["generated_at"] = generated.isoformat()
    assert provider_manifest_valid_until("abliterate", raw) == EXPIRED_PROVIDER_MANIFEST
    assert provider_manifest_canary_quarantine_valid_until("abliterate", raw) == generated + timedelta(days=14)
    raw["models"][0]["routable"] = True
    assert provider_manifest_canary_quarantine_valid_until("abliterate", raw) == EXPIRED_PROVIDER_MANIFEST
