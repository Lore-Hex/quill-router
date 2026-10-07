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


def test_reviewed_estimated_routes_require_successful_canary(monkeypatch, tmp_path):
    adapter = _direct_openai.DirectOpenAIProvider(abliterate.CATALOG.spec, manifest_path=tmp_path / "abliterate.json")
    monkeypatch.setenv("ABLITERATE_API_KEY", "test-only-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: {
        "data": [{"id": name} for name in abliterate.EXPLICIT_MODEL_MAP] + [{"id": "abliterate-new"}]
    })
    monkeypatch.setattr(adapter, "_joined_prices", lambda: abliterate._parse_prices(docs_prices()))
    checked = []

    def canary(**kwargs):
        checked.append(kwargs)
        return kwargs["model"] != "abliterate-0.3-clever"

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", canary)
    for _ in range(2):
        result = adapter.fetch()
        adapter.write_provider_manifest(result)
        manifest = json.loads(adapter.manifest_path.read_text())
        assert len(manifest["models"]) == 4
        for row in manifest["models"]:
            assert row["routable"] is (row["upstream_id"] in {"abliterate-0.3-fast", "abliterate-0.3-balanced"})
            if row["id"] in abliterate.OPERATOR_HOLDS:
                assert row["routable_reason"] == "upstream-output-limit-unenforced"
            elif row["routable"] is False:
                assert row["routable_reason"] == "provider-canary-failed"
            assert row["upstream_id"] in abliterate.EXPLICIT_MODEL_MAP
            assert row.get("context_length") is None
    assert {check["model"] for check in checked} == set(abliterate.EXPLICIT_MODEL_MAP) - {"abliterated-research-0.1"}
    assert all(check["require_message"] is True for check in checked)
    assert all(check["require_usage"] is False for check in checked)


def test_registration_privacy_secret_and_reviewed_live_routes():
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
    assert all(
        endpoint.model_id in abliterate.EXPLICIT_MODEL_MAP.values()
        for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "abliterate"
    )
    assert OPENAI_COMPATIBLE_PROVIDERS["abliterate"] == (("ABLITERATE_API_KEY",), abliterate.BASE_URL)
    assert default_provider_secret_ref("abliterate") == "env://ABLITERATE_API_KEY"
    assert "abliterate_api_key" in SENSITIVE_STRING_FRAGMENTS
    root = Path(__file__).resolve().parents[1]
    assert "ABLITERATE_API_KEY:trustedrouter-abliterate-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()


def test_failed_canaries_are_quarantined_not_routable():
    from datetime import UTC, datetime, timedelta

    from trusted_router.provider_manifest_policy import (
        EXPIRED_PROVIDER_MANIFEST,
        provider_manifest_canary_quarantine_valid_until,
        provider_manifest_valid_until,
    )

    raw = json.loads(abliterate.MANIFEST_PATH.read_text())
    for row in raw["models"]:
        row["routable"] = False
        row["routable_reason"] = "provider-canary-failed"
    generated = datetime.now(UTC)
    raw["generated_at"] = generated.isoformat()
    assert provider_manifest_valid_until("abliterate", raw) == EXPIRED_PROVIDER_MANIFEST
    assert provider_manifest_canary_quarantine_valid_until("abliterate", raw) == generated + timedelta(days=14)
    raw["models"][0]["routable"] = True
    assert provider_manifest_canary_quarantine_valid_until("abliterate", raw) == EXPIRED_PROVIDER_MANIFEST


def test_estimated_billing_disclosed_without_changing_published_prices():
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, model_to_openrouter_shape
    from trusted_router.catalog_usage_policy import (
        ABLITERATE_ESTIMATED_USAGE_NOTICE,
        provider_usage_estimation_policy,
    )
    from trusted_router.provider_branding import PROVIDER_BRANDS

    policy = provider_usage_estimation_policy("abliterate")
    assert policy is not None
    assert policy["maximum_estimate_multiplier"] == 2
    assert policy["authorized_budget_bounded"] is True
    assert policy["prepaid_only"] is True
    assert policy["provider_usage_preferred"] is True
    assert ABLITERATE_ESTIMATED_USAGE_NOTICE in PROVIDER_BRANDS["abliterate"].description
    assert provider_usage_estimation_policy("sambanova") is None
    expected = set(abliterate.EXPLICIT_MODEL_MAP.values()) - abliterate.OPERATOR_HOLDS.keys()
    assert {endpoint.model_id for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "abliterate"} == expected
    prices = abliterate._parse_prices(docs_prices())
    for model in MODELS.values():
        if model.provider != "abliterate":
            continue
        payload = model_to_openrouter_shape(model)
        assert payload["trustedrouter"]["usage_estimation"] == policy
        assert ABLITERATE_ESTIMATED_USAGE_NOTICE in payload["description"]
        # Existing 5.5% platform pricing remains unchanged; no hidden 2x rate.
        assert model.published_prompt_price_microdollars_per_million_tokens == prices[model.id].prompt_micro_per_m * 1055 // 1000
        assert model.published_completion_price_microdollars_per_million_tokens == prices[model.id].completion_micro_per_m * 1055 // 1000


def test_model_page_displays_estimated_billing_notice():
    from bs4 import BeautifulSoup

    from trusted_router.catalog_usage_policy import ABLITERATE_ESTIMATED_USAGE_NOTICE
    from trusted_router.config import Settings
    from trusted_router.dashboard import public_model_detail_html

    html = public_model_detail_html(Settings(environment="test"), "abliterate/abliterate-0.3-fast")
    assert html is not None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    assert "Estimated usage" in text
    assert ABLITERATE_ESTIMATED_USAGE_NOTICE in text


def test_missing_or_ambiguous_prices_still_stop_activation(monkeypatch, tmp_path):
    adapter = _direct_openai.DirectOpenAIProvider(abliterate.CATALOG.spec, manifest_path=tmp_path / "abliterate.json")
    monkeypatch.setenv("ABLITERATE_API_KEY", "test-only-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: {
        "data": [{"id": name} for name in abliterate.EXPLICIT_MODEL_MAP]
    })
    monkeypatch.setattr(adapter, "_joined_prices", lambda: abliterate._parse_prices(""))
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **_kw: pytest.fail("unpriced probes must not spend"))
    with pytest.raises(RuntimeError, match="incomplete"):
        adapter.fetch()
    assert not adapter.manifest_path.exists()
