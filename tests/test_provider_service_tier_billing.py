"""Provider-reported tiers must not select another provider's tariff."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from tests.pinned_manifests import serve_manifest_rows
from trusted_router.catalog import endpoint_for_id
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
from trusted_router.storage import STORE, configure_store
from trusted_router.typed_balance import live_credit_summary


@pytest.mark.parametrize("provider", ["atlas-cloud", "anthropic", "vertex", "together"])
def test_priority_report_preserves_non_openai_endpoint_tariff(provider: str) -> None:
    endpoint = ModelEndpoint(
        id=f"tier-regression:{provider}", model_id="openai/gpt-5.6-sol",
        provider=provider, usage_type="credits",
        prompt_price_microdollars_per_million_tokens=2_000_000,
        completion_price_microdollars_per_million_tokens=4_000_000,
    )
    kwargs = {"cache_read_tokens": 100, "cache_creation_tokens": 20}
    ordinary = _endpoint_cost_microdollars(endpoint, 500, 30, **kwargs)
    # The publisher in model_id is not the billing provider. This must not
    # dispatch to OpenAI's premium tariff even for an OpenAI model name.
    assert ordinary > 0
    assert _endpoint_cost_microdollars(
        endpoint, 500, 30, service_tier="priority", **kwargs
    ) == ordinary


@pytest.fixture
def atlas_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Pinned from Atlas's 2026-09-30 /v1/models tariff, not hourly live data.
    serve_manifest_rows(monkeypatch, tmp_path, "atlas-cloud", [{
        "id": "openai/gpt-5.6-sol-az", "upstream_id": "openai/gpt-5.6-sol-az",
        "display_name": "GPT 5.6 Sol", "model_type": "chat", "status": 1,
        "input_modalities": ["text"], "output_modalities": ["text"],
        "endpoints": ["chat/completions"], "context_length": 1050000,
        "max_output_tokens": 131072, "input_token_price_per_m": 5_000_000,
        "output_token_price_per_m": 30_000_000,
        "cached_input_token_price_per_m": 500_000,
    }])


@pytest.mark.usefixtures("atlas_route")
@pytest.mark.parametrize("backend", ["memory", "spanner"])
@pytest.mark.parametrize("operation", ["settle", "refund"])
def test_atlas_priority_finalizes_once_at_its_own_price(backend: str, operation: str) -> None:
    if backend == "spanner":
        store, _ = make_fake_store(
            request_record_write_mode="typed", generation_records_enabled=True,
        )
        configure_store(store)
    app = create_app(
        Settings(environment="test"), configure_store_arg=backend == "memory",
        init_observability=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        workspace = STORE.create_workspace(
            "tier-owner", "tier-regression", trial_credit_microdollars=1_000_000,
        )
        _raw, key = STORE.create_api_key(
            workspace_id=workspace.id, name="tier-regression", creator_user_id="tier-owner",
        )
        authorize = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": key.hash, "model": "openai/gpt-5.6-sol-az",
            "provider": {"only": ["atlas-cloud"], "allow_fallbacks": False},
            "estimated_input_tokens": 1_000, "max_output_tokens": 100,
        })
        assert authorize.status_code == 200, authorize.text
        auth = authorize.json()["data"]
        assert auth["provider"] == "atlas-cloud"
        endpoint = endpoint_for_id(auth["endpoint_id"])
        assert endpoint is not None
        authorization = STORE.get_gateway_authorization(auth["authorization_id"])
        assert authorization is not None
        before = live_credit_summary(authorization.workspace_id)
        assert before is not None
        usage_before = before["total_usage"]
        # OpenAI-compatible input count includes its cached subset.
        expected = _endpoint_cost_microdollars(
            endpoint, 100, 20, cache_read_tokens=900, effective_at=authorization.created_at,
        ) if operation == "settle" else 0
        body = {
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1_000, "actual_output_tokens": 20,
            "cache_read_input_tokens": 900, "service_tier": "priority",
            "request_id": f"tier-{backend}-{operation}", "elapsed_seconds": 1.0,
        }
        first = client.post(f"/v1/internal/gateway/{operation}", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["data"]["cost_microdollars"] == expected
        after = live_credit_summary(authorization.workspace_id)
        assert after is not None
        assert after["total_usage"] - usage_before == expected
        assert after["reserved"] == 0
        replay = client.post(f"/v1/internal/gateway/{operation}", json=body)
        assert replay.status_code == 200, replay.text
        replay_account = live_credit_summary(authorization.workspace_id)
        assert replay_account is not None
        assert replay_account["total_usage"] == usage_before + expected
        assert replay_account["reserved"] == 0
        if operation == "settle":
            generation = STORE.get_generation(first.json()["data"]["generation_id"])
            assert generation is not None
            assert generation.provider == "atlas-cloud"
            assert generation.tokens_prompt == 1_000
            assert generation.tokens_completion == 20
