from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.pinned_manifests import OPENAI_GPT_5_5, serve_manifest_rows
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    endpoint_meets_privacy_requirement,
    endpoint_zero_data_retention,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
)
from trusted_router.catalog_privacy import endpoint_retention_metadata
from trusted_router.config import Settings
from trusted_router.dashboard import _env, _provider_model_rows
from trusted_router.managed_retention import (
    OPENAI_PSP,
    OPENAI_ZDR,
    REVIEWED_ROUTES,
    managed_retention_policy,
)
from trusted_router.routing import chat_route_endpoint_candidates


@pytest.mark.parametrize("provider,model,upstream,mode", [
    ("openai", "openai/gpt-4.1", "gpt-4.1", "zero_retention"),
    ("openai", "openai/gpt-5.5", "gpt-5.5", "durable_cache"),
    ("openai", "openai/gpt-6.1-sol", "gpt-6.1-sol", "durable_cache"),
    ("openai", "openai/gpt-new", "gpt-new", "unverified"),
    ("openai", "openai/gpt-5.5", "gpt-new", "unverified"),
    ("openai", "openai/chat-latest", "chat-latest", "unverified"),
    ("openai", "openai/sora-2", "sora-2", "unverified"),
    ("google-vertex", "google/gemini-2.5-flash", "gemini-2.5-flash", "zero_retention"),
    ("google-vertex", "google/gemini-3.8-flash", "gemini-3.8-flash", "durable_cache"),
    ("google-vertex", "google/gemini-4-argon", "gemini-4-argon", "durable_cache"),
    ("google-vertex", "google/gemini-4-argon-cyber", "gemini-4-argon-cyber", "safety_retention"),
    ("google-vertex", "anthropic/claude-fable-5.1", "claude-fable-5-1", "safety_retention"),
    ("google-vertex", "anthropic/claude-mythos", "claude-mythos", "safety_retention"),
    ("google-vertex", "anthropic/claude-opus-5.5", "claude-opus-5-5", "unverified"),
    ("google-vertex", "google/gemini-3.8-tts", "gemini-3.8-tts", "unverified"),
])
def test_exact_route_retention(provider: str, model: str, upstream: str, mode: str) -> None:
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider=provider,
                       model_id=model, upstream_id=upstream, usage_type="Credits")
    metadata = endpoint_retention_metadata(endpoint)
    assert metadata is not None
    assert metadata["mode"] == mode
    assert metadata["strict_zdr_eligible"] is (mode == "zero_retention")
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_STANDARD)
    for requirement in (PRIVACY_TIER_NO_STORE, PRIVACY_TIER_ZERO_RETENTION):
        assert endpoint_meets_privacy_requirement(endpoint, requirement) is (mode == "zero_retention")
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)


def test_byok_never_inherits_reviewed_managed_account() -> None:
    policy = managed_retention_policy("openai", "openai/gpt-5.5", "gpt-5.5", "BYOK")
    assert policy is not None and not policy.permits_strict_zdr
    assert managed_retention_policy("tinfoil", "model", "model", "Credits") is None


def test_review_does_not_upgrade_an_account_without_zdr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROVIDERS, "openai", replace(PROVIDERS["openai"], prepaid_zero_data_retention=False))
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="openai",
                       model_id="openai/gpt-4.1", upstream_id="gpt-4.1", usage_type="Credits")
    assert not endpoint_zero_data_retention(endpoint)


@pytest.fixture
def psp_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Test a future explicit PSP review without declaring any live account PSP.
    monkeypatch.setitem(REVIEWED_ROUTES, ("openai", "openai/gpt-5.5", "gpt-5.5"), OPENAI_PSP)
    serve_manifest_rows(monkeypatch, tmp_path, "openai", [OPENAI_GPT_5_5])


def test_psp_metadata_distinguishes_encryption_from_retention(psp_route: None) -> None:
    assert PROVIDERS["openai"].prepaid_zero_data_retention
    shape = model_to_openrouter_shape(MODELS["openai/gpt-5.5"])
    for endpoint in shape["trustedrouter"]["endpoints"]:
        if endpoint["provider"] != "openai":
            continue
        assert endpoint["provider_zero_data_retention"] is False
        assert endpoint["retention"]["strict_zdr_eligible"] is False
        if endpoint["usage_type"] == "Credits":
            assert endpoint["retention"]["mode"] == "private_safety_processing"
            assert endpoint["retention"]["minimum_days"] == 30
            assert endpoint["retention"]["maximum_days"] is None
            assert endpoint["retention"]["human_review"] is False


@pytest.mark.parametrize("model", ["openai/gpt-5.5", "trustedrouter/auto", "trustedrouter/zdr"])
def test_psp_excluded_from_direct_and_pool_routes(psp_route: None, model: str) -> None:
    from fastapi import HTTPException

    try:
        candidates = chat_route_endpoint_candidates(
            {"model": model, "provider": {"min_privacy": "zdr", "only": ["openai"]}},
            Settings(environment="test"),
        )
    except HTTPException as exc:
        assert exc.status_code in {400, 404, 503}
    else:
        assert not any(endpoint.model_id == "openai/gpt-5.5" for _, endpoint in candidates)


def test_psp_remains_available_without_privacy_floor(psp_route: None) -> None:
    candidates = chat_route_endpoint_candidates(
        {"model": "openai/gpt-5.5", "provider": {"only": ["openai"]}},
        Settings(environment="test"),
    )
    assert candidates


def test_psp_rejected_from_existing_authorization_and_gateway_snapshot(psp_route: None) -> None:
    from fastapi import HTTPException

    from trusted_router.routes.internal.gateway import _authorization_endpoint_candidates
    from trusted_router.routing_snapshot import _endpoint_entry

    endpoint = next(endpoint for endpoint in MODEL_ENDPOINTS.values()
                    if endpoint.provider == "openai" and endpoint.model_id == "openai/gpt-5.5"
                    and endpoint.usage_type == "Credits")
    authorization = SimpleNamespace(user_provided_model_id=None,
                                    candidate_endpoint_ids=[endpoint.id], endpoint_id=endpoint.id)
    with pytest.raises(HTTPException) as exc:
        _authorization_endpoint_candidates(
            authorization, [], privacy_requirements=frozenset({PRIVACY_TIER_ZERO_RETENTION}),
        )
    assert exc.value.status_code == 409
    entry = _endpoint_entry(endpoint, [1793145600])
    assert PRIVACY_TIER_ZERO_RETENTION not in entry.privacy.meets
    assert PRIVACY_TIER_NO_STORE not in entry.privacy.meets
    assert entry.privacy.stores_content is True


def test_psp_provider_and_model_pages_show_retention(psp_route: None, client: TestClient) -> None:
    rows = _provider_model_rows("openai", test_mode=True)
    row = next(row for row in rows if row["id"] == "openai/gpt-5.5")
    assert row["retention_policies"][0]["mode"] == "private_safety_processing"
    for url in ("/providers/openai", "/models/openai/gpt-5.5"):
        response = client.get(url)
        assert response.status_code == 200
        assert 'data-retention="private_safety_processing"' in response.text
        assert "Private safety processing (PSP)" in response.text
    shape = provider_to_openrouter_shape(PROVIDERS["openai"])
    assert shape["retention_requires_model_review"] is True
    assert "Future models" in shape["retention_notice"]


def test_retention_badge_is_not_an_unqualified_zdr_badge() -> None:
    template = _env().get_template("public/_provider_privacy_badges.html")
    html = template.module.provider_privacy_badges({
        "retention": {**OPENAI_PSP.metadata(), "strict_zdr_eligible": False},
        "provider_zero_data_retention": False,
    })
    assert "Private safety processing (PSP)" in html
    assert 'data-privacy="zdr"' not in html
    assert OPENAI_ZDR.permits_strict_zdr
