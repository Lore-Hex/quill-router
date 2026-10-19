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
    assert not provider.provider_zero_data_retention
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("HIXTUBE_API_KEY",), p.BASE_URL)
    assert any(row[0] == p.SLUG for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    root = Path(__file__).resolve().parents[1]
    assert "HIXTUBE_API_KEY:trustedrouter-hixtube-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "HIXTUBE_API_KEY" "trustedrouter-hixtube-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()
