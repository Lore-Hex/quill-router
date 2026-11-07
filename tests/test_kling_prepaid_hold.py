from __future__ import annotations

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS


def test_kling_operator_balance_hold_survives_fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PREPAID_PROVIDER_HOLD_REASONS.get("kling") == "operator-credit-exhausted"
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    model = "kling/v3-pro"
    endpoints = {
        f"{slug}/{usage}": ModelEndpoint(
            id=f"{slug}/{usage}", model_id=model, provider=slug,
            usage_type=usage, upstream_id=model,
        )
        for slug in ("kling", "other-provider") for usage in ("Credits", "BYOK")
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, explicit_model_ids=frozenset({model}),
    )
    assert filtered == {key: row for key, row in endpoints.items() if key != "kling/Credits"}


def test_kling_hold_blocks_new_admission_but_preserves_existing_settlement() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS, endpoint_for_id
    from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat

    assert "kling" in PROVIDERS
    assert not [row for row in MODEL_ENDPOINTS.values()
                if row.provider == "kling" and row.usage_type == "Credits"]
    held = [row for row in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values() if row.provider == "kling"]
    assert {row.model_id for row in held} == {"kling/v3-pro", "kling/o3-pro"}
    for row in held:
        assert endpoint_for_id(row.id) is None
        assert _endpoint_for_id_compat(row.id) == row
    assert any(row.provider == "venice" and row.model_id == "kling/v3-pro"
               for row in MODEL_ENDPOINTS.values())


def test_daily_video_canary_skips_held_provider_without_adding_generations() -> None:
    from datetime import date

    from trusted_router.synthetic.video_generation import daily_video_profile

    assert daily_video_profile(date(2026, 8, 6)).provider == "ltx"
    assert daily_video_profile(date(2026, 8, 7)).provider == "ltx"
    assert daily_video_profile(date(2026, 8, 3)).provider == "grok"


def test_daily_video_canary_fails_explicitly_when_all_providers_are_held(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from trusted_router.synthetic import video_generation

    monkeypatch.setattr(video_generation, "PREPAID_PROVIDER_HOLD_REASONS", {
        profile.provider: "operator-credit-exhausted" for profile in video_generation.DAILY_VIDEO_PROFILES
    })
    with pytest.raises(RuntimeError, match="No unheld prepaid video provider"):
        video_generation.daily_video_profile(date(2026, 8, 3))
