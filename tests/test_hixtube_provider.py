from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing.providers import hixtube as p


def publish():
    from scripts.pricing.refresh import _write_provider_manifests

    notes, failures = _write_provider_manifests({p.SLUG: p.fetch()})
    assert not failures
    assert notes


def catalog():
    return {"object": "list", "contract_version": "2.0", "provider": {
        "id": "hixtube", "status_url": "https://api.hixtube.com/healthz",
        "support_contact": "mailto:support@trustedrouter.com",
        "incident_contact": "mailto:support@trustedrouter.com",
        "regions": ["US"], "request_id_header": "X-Request-Id",
        "error_contract": {"rate_limit_status": 429, "overload_status": 503,
                           "retry_after_header": "Retry-After", "account_quota_error_codes": []},
    }, "data": [{
        "id": "qwen/qwen3-0.6b", "object": "model", "owned_by": "qwen",
        "name": "Qwen3 0.6B (Hixtube CPU pilot)", "type": "chat",
        "context_length": 4096, "max_output_tokens": 256,
        "endpoints": ["chat/completions"], "input_modalities": ["text"], "output_modalities": ["text"],
        "capabilities": {"streaming": True, "tools": False, "reasoning": False,
                         "structured_output": False, "prompt_caching": False},
        "pricing": {"currency": "USD", "unit": "per_1m_tokens", "input": "0.10", "output": "0.10",
                    "cached_input": None, "cache_write": None, "minimum_request": "0"},
        "lifecycle": {"status": "active", "deprecation_at": None,
                      "retirement_at": (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                      "replacement_model_id": None},
        "reliability": {"first_token_timeout_seconds": 30, "completion_timeout_seconds": 120,
                        "stream_idle_timeout_seconds": 30, "capacity_scope": "model"},
    }]}


def test_catalog_preserves_price_capacity_and_native_identity():
    prices, rows = p.discover(catalog())
    price = prices["qwen/qwen3-0.6b"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (100000, 100000)
    row = rows["qwen/qwen3-0.6b"]
    assert row["upstream_id"] == "qwen/qwen3-0.6b"
    assert row["context_length"] == 4096
    assert row["max_output_tokens"] == 256
    assert "vision" not in row["supported_features"]
    reliability = row["provider_reliability"]
    assert reliability["provider_id"] == "hixtube"
    assert reliability["regions"] == ["US"]
    assert row["reliability"]["completion_timeout_seconds"] == 120
    assert "support_contact" not in reliability and "incident_contact" not in reliability


def test_catalog_v2_contract_requires_full_reliability():
    payload = catalog()
    del payload["data"][0]["reliability"]["capacity_scope"]
    with pytest.raises(RuntimeError, match="capacity_scope"):
        p.discover(payload)


def test_legacy_catalog_without_provider_reliability_is_rejected():
    payload = catalog()
    del payload["contract_version"]
    del payload["provider"]
    del payload["data"][0]["reliability"]
    with pytest.raises(RuntimeError, match="Catalog v2"):
        p.discover(payload)


@pytest.mark.parametrize("version", [None, "1.0", "2", 2, 2.0])
def test_catalog_requires_exact_v2_before_shared_parsing(monkeypatch, version):
    payload = catalog()
    payload["contract_version"] = version

    def unexpected_parse(*_args, **_kwargs):
        pytest.fail("invalid contract reached shared parser")

    monkeypatch.setattr(p, "discover_provider_contract_catalog", unexpected_parse)
    with pytest.raises(RuntimeError, match="Catalog v2"):
        p.discover(payload)


@pytest.mark.parametrize("provider", [None, {}, {"id": "unrelated"}, {"id": "Hixtube"}, {"id": None}])
def test_catalog_requires_exact_provider_identity_before_shared_parsing(monkeypatch, provider):
    payload = catalog()
    payload["provider"] = provider

    def unexpected_parse(*_args, **_kwargs):
        pytest.fail("invalid provider identity reached shared parser")

    monkeypatch.setattr(p, "discover_provider_contract_catalog", unexpected_parse)
    with pytest.raises(RuntimeError, match="provider.id"):
        p.discover(payload)


def test_v2_catalog_cannot_import_another_provider_identity():
    payload = catalog()
    payload["provider"]["id"] = "unrelated"
    with pytest.raises(RuntimeError, match="provider.id"):
        p.discover(payload)


def test_selected_deepseek_catalog_preserves_variant_limits_and_prices():
    payload = catalog()
    model_id = "dealignai/deepseek-v4.1-flash-1017-uncensored-fp8"
    payload["provider"]["regions"] = ["FI"]
    payload["data"][0].update(
        id=model_id, owned_by="dealignai", name="Selected DeepSeek checkpoint",
        context_length=8192, max_output_tokens=256,
    )
    payload["data"][0]["pricing"].update(input="0.65", output="1.45")
    cutoff = payload["data"][0]["lifecycle"]["retirement_at"]

    prices, rows = p.discover(payload)

    assert set(prices) == set(rows) == {model_id}
    price, row = prices[model_id], rows[model_id]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (650000, 1450000)
    assert row["upstream_id"] == model_id
    assert (row["context_length"], row["max_output_tokens"]) == (8192, 256)
    assert row["retirement_at"] == cutoff
    assert row["provider_reliability"]["regions"] == ["FI"]
    assert "vision" not in row["supported_features"]


def test_successful_v2_fetch_publishes_only_canary_verified_route(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is True
    assert row["upstream_id"] == "qwen/qwen3-0.6b"
    assert row["max_output_tokens"] == 256
    assert row["provider_reliability"]["provider_id"] == "hixtube"


@pytest.mark.parametrize("value", [None, "bad", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_pilot_requires_finite_future_retirement(value):
    payload = catalog()
    payload["data"][0]["lifecycle"]["retirement_at"] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


@pytest.mark.parametrize("seconds,valid", [(0, False), (86400, True), (604800, True), (604801, False)])
def test_retirement_is_bounded_to_one_week(monkeypatch, seconds, valid):
    now = datetime(2026, 10, 27, tzinfo=UTC)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(p, "datetime", Fixed)
    row = {"retirement_at": (now + timedelta(seconds=seconds)).isoformat()}
    if valid:
        assert p._retirement(row) == now + timedelta(seconds=seconds)
    else:
        with pytest.raises(RuntimeError):
            p._retirement(row)


def test_runtime_cutoff_survives_refresh_without_extension(monkeypatch, tmp_path):
    from trusted_router.provider_manifest_policy import provider_manifest_valid_until

    payload = catalog()
    cutoff = payload["data"][0]["lifecycle"]["retirement_at"]
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: payload)
    probes = []
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: probes.append(kw) or True)
    for _ in range(2):
        p.write_provider_manifest(p.fetch())
        raw = json.loads(p.MANIFEST_PATH.read_text())
        assert raw["pricing_valid_until"] == cutoff
        assert provider_manifest_valid_until("hixtube", raw) == datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    assert len(probes) == 2


def test_failed_candidate_write_does_not_damage_last_known_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    p.write_provider_manifest(p.fetch())
    before = p.MANIFEST_PATH.read_bytes()

    def fail_write(*_args, manifest_path, **_kwargs):
        assert manifest_path != p.MANIFEST_PATH
        manifest_path.write_text("partial")
        raise OSError("write failed")

    monkeypatch.setattr(p, "write_discovered_chat_manifest", fail_write)
    with pytest.raises(OSError):
        p.write_provider_manifest(p.fetch())
    assert p.MANIFEST_PATH.read_bytes() == before
    assert list(tmp_path.iterdir()) == [p.MANIFEST_PATH]


def test_failed_repeat_probe_cannot_be_undone_by_mass_prune_guard(monkeypatch, tmp_path):
    from trusted_router.provider_manifest_policy import (
        EXPIRED_PROVIDER_MANIFEST,
        provider_manifest_valid_until,
    )

    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    publish()
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: False)
    publish()
    raw = json.loads(p.MANIFEST_PATH.read_text())
    assert raw["models"][0]["routable"] is False
    assert raw["models"][0]["routable_reason"] == "provider-canary-failed"
    assert provider_manifest_valid_until("hixtube", raw) == EXPIRED_PROVIDER_MANIFEST
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    publish()
    raw = json.loads(p.MANIFEST_PATH.read_text())
    assert raw["models"][0]["routable"] is True
    assert "routable_reason" not in raw["models"][0]
    assert provider_manifest_valid_until("hixtube", raw) > datetime.now(UTC)


def test_expired_missing_model_does_not_block_healthy_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    payload = catalog()
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: payload)
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    publish()
    later = datetime.now(UTC) + timedelta(hours=3)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz) if tz else later.replace(tzinfo=None)

    monkeypatch.setattr(p, "datetime", Later)
    payload = catalog()
    payload["data"][0]["id"] = "qwen/replacement"
    payload["data"][0]["lifecycle"]["retirement_at"] = (later + timedelta(hours=2)).isoformat()
    for _ in range(2):
        publish()
        rows = {row["id"]: row for row in json.loads(p.MANIFEST_PATH.read_text())["models"]}
        assert rows["qwen/replacement"]["routable"] is True
        if "qwen/qwen3-0.6b" in rows:
            assert rows["qwen/qwen3-0.6b"]["routable"] is False
            assert rows["qwen/qwen3-0.6b"].get("missing_since") is not None

    # A renewed, probed pilot may recover its machine-owned retirement hold.
    payload["data"][0]["id"] = "qwen/qwen3-0.6b"
    publish()
    rows = {row["id"]: row for row in json.loads(p.MANIFEST_PATH.read_text())["models"]}
    assert rows["qwen/qwen3-0.6b"]["routable"] is True
    assert "routable_reason" not in rows["qwen/qwen3-0.6b"]
    assert "missing_since" not in rows["qwen/qwen3-0.6b"]


@pytest.mark.parametrize("reason", [None, "operator-safety-hold"])
def test_publication_preserves_operator_hold_even_after_healthy_probe(monkeypatch, tmp_path, reason):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    publish()
    raw = json.loads(p.MANIFEST_PATH.read_text())
    raw["models"][0]["routable"] = False
    if reason:
        raw["models"][0]["routable_reason"] = reason
    p.MANIFEST_PATH.write_text(json.dumps(raw))
    publish()
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row.get("routable_reason") == reason


@pytest.mark.parametrize("reason, explicit", [(None, False), (None, True), ("operator-safety-hold", True)])
def test_publication_preserves_operator_hold_through_failed_then_healthy_probe(monkeypatch, tmp_path, reason, explicit):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    publish()
    raw = json.loads(p.MANIFEST_PATH.read_text())
    held = raw["models"][0]
    held["routable"] = False
    if explicit:
        held["routable_reason"] = reason
    else:
        held.pop("routable_reason", None)
    p.MANIFEST_PATH.write_text(json.dumps(raw))

    for healthy in (False, True):
        monkeypatch.setattr(p, "probe_openai_chat", lambda healthy=healthy, **_kw: healthy)
        publish()
        row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
        assert row["routable"] is False
        assert row.get("routable_reason") == reason
        if reason is None:
            assert row == {key: value for key, value in held.items() if key != "routable_reason"}


def test_quarantine_confirmation_requires_current_evidence(monkeypatch):
    from scripts.pricing.base import ProviderPricingResult

    monkeypatch.setattr(p, "_DISCOVERED_ROWS", {})
    before = [{"id": "a", "retirement_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}]
    retired = [{**before[0], "routable": False, "routable_reason": "pilot-retired"}]
    result = ProviderPricingResult(slug=p.SLUG, prices={}, source="api")
    assert not p.confirmed_manifest_quarantines(result, before, retired)
    retired[0]["retirement_at"] = "2026-01-01T00:00:00Z"
    assert not p.confirmed_manifest_quarantines(result, before, retired)
    failed = [{"id": "a", "routable": False, "routable_reason": "provider-canary-failed"}]
    assert not p.confirmed_manifest_quarantines(result, before, failed)
    before[0]["retirement_at"] = retired[0]["retirement_at"]
    result.source = "stale_snapshot"
    assert not p.confirmed_manifest_quarantines(result, before, retired)


@pytest.mark.parametrize("value", ["NaN", "-1", None, 0.1])
def test_invalid_price_cannot_publish(value):
    payload = catalog()
    payload["data"][0]["pricing"]["input"] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_missing_key_failed_probe_and_stale_fetch_are_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    calls = []
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"
    assert calls[0]["require_usage"] is True and calls[0]["max_tokens"] <= 256
    monkeypatch.delenv("HIXTUBE_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        p.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        p.write_provider_manifest(result)


def test_registration_never_claims_confidentiality():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    provider = PROVIDERS[p.SLUG]
    assert provider.supports_prepaid and not provider.supports_byok
    assert provider.provider_zero_data_retention
    assert not provider.stores_content
    assert provider.provider_policy_url == "https://hixtube.com/privacy.html"
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("HIXTUBE_API_KEY",), p.BASE_URL)
    assert any(row[0] == p.SLUG for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    root = Path(__file__).resolve().parents[1]
    assert "HIXTUBE_API_KEY:trustedrouter-hixtube-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "HIXTUBE_API_KEY" "trustedrouter-hixtube-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()


def test_zdr_is_enforced_without_granting_confidential_or_trusted():
    from trusted_router.catalog_data import (
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_TRUSTED,
        PRIVACY_TIER_ZERO_RETENTION,
        ModelEndpoint,
    )
    from trusted_router.catalog_privacy import (
        endpoint_meets_privacy_requirement,
        endpoint_privacy_tier,
        endpoint_stores_content,
        endpoint_zero_data_retention,
    )

    endpoint = ModelEndpoint(
        id="hixtube-zdr-fixture", model_id="dealignai/deepseek-v4.1-flash-1017-uncensored-fp8",
        provider="hixtube", usage_type="Credits",
    )
    assert endpoint_zero_data_retention(endpoint) is True
    assert not endpoint_stores_content(endpoint)
    assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_ZERO_RETENTION
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_TRUSTED)


def test_missing_manifest_expires_own_routes_without_blocking_other_providers(monkeypatch, tmp_path):
    from scripts import check_price_coverage
    from trusted_router import catalog as catalog_module
    from trusted_router import catalog_ingest
    from trusted_router.provider_manifest_policy import EXPIRED_PROVIDER_MANIFEST

    monkeypatch.setattr(catalog_module, "GATEWAY_PREPAID_PROVIDER_SLUGS", {p.SLUG})
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(check_price_coverage, "_OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS", {p.SLUG})
    monkeypatch.setattr(check_price_coverage, "EXPIRING_PROVIDER_MANIFEST_SLUGS", {p.SLUG})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(catalog_ingest, "_EXPIRING_PROVIDER_MANIFEST_SLUGS", {p.SLUG})
    warnings, _info, hard_failures = check_price_coverage.audit(
        max_age_days=14, now=datetime.now(UTC), check_model_discovery=False,
    )
    assert any(row.startswith("hixtube: NO price source") for row in warnings)
    assert not hard_failures
    assert catalog_ingest.provider_manifest_deadlines()[p.SLUG] == EXPIRED_PROVIDER_MANIFEST
    assert list(tmp_path.iterdir()) == []
