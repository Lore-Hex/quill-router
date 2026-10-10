from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 10, 22, tzinfo=UTC)
_ROUTES = {
    "google/veo-3.1": "veo-3.1-generate-preview",
    "google/veo-3.1-fast": "veo-3.1-fast-generate-preview",
}


def test_veo_preview_cutoff_is_midnight_utc() -> None:
    assert provider_lifecycle.GOOGLE_AI_STUDIO_VEO_PREVIEW_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model", "native"), _ROUTES.items())
def test_ai_studio_veo_preview_routed_before_cutover_retired_after(
    monkeypatch: pytest.MonkeyPatch, model: str, native: str,
) -> None:
    assert catalog.MODELS[model].supports_video
    retired = provider_lifecycle.provider_model_retired
    assert not retired("google-ai-studio", model, native, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("google-ai-studio", model, native, at=_CUTOFF)
    assert not retired("google-vertex", model, native.replace("-preview", "-001"), at=_CUTOFF)
    endpoints = {}
    for slug, upstream in (("google-ai-studio", native), ("other-video-host", native)):
        endpoint = ModelEndpoint(
            id=f"{model}@{slug}/prepaid", model_id=model, provider=slug,
            usage_type="Credits", upstream_id=upstream,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert {row.provider for row in catalog.endpoints_for_model(model)} == {
        "google-ai-studio", "other-video-host",
    }
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert [row.provider for row in catalog.endpoints_for_model(model)] == ["other-video-host"]


def test_video_models_honor_retirements_like_every_other_model() -> None:
    # Video routes were once exempt from the lifecycle filter, so a scheduled
    # retirement published a date but kept routing to the dead upstream.
    later = datetime(2030, 1, 1, tzinfo=UTC)
    for endpoint in catalog.MODEL_ENDPOINTS.values():
        model = catalog.MODELS.get(endpoint.model_id)
        if model is None or not model.supports_video:
            continue
        retired = provider_lifecycle.provider_model_retired(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=later,
        )
        if endpoint.provider == "google-ai-studio" and endpoint.model_id in _ROUTES:
            assert retired, endpoint.id
        else:
            assert not retired, endpoint.id


def test_venice_omni_flash_is_not_the_retired_preview() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "venice", "google/gemini-omni-flash", "gemini-omni-flash-text-to-video", at=_CUTOFF,
    )


_FLASH_CUTOFF = datetime(2026, 10, 10, tzinfo=UTC)


def test_ai_studio_gemini_35_flash_retires_while_other_hosts_stay() -> None:
    assert provider_lifecycle.GOOGLE_AI_STUDIO_GEMINI_35_FLASH_RETIREMENT_AT == _FLASH_CUTOFF
    retired = provider_lifecycle.provider_model_retired
    model = "google/gemini-3.5-flash"
    assert not retired("google-ai-studio", model, "gemini-3.5-flash", at=_FLASH_CUTOFF - timedelta(microseconds=1))
    assert retired("google-ai-studio", model, "gemini-3.5-flash", at=_FLASH_CUTOFF)
    for provider, upstream in (
        ("google-vertex", "gemini-3.5-flash"), ("gmi", model), ("atlas-cloud", model),
    ):
        assert not retired(provider, model, upstream, at=_FLASH_CUTOFF)
    assert not retired("google-ai-studio", "google/gemini-3.6-flash", "gemini-3.6-flash", at=_FLASH_CUTOFF)
    [entry] = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.effective_at == _FLASH_CUTOFF and model in entry.model_ids
    ]
    assert entry.replacement_model_ids == ("google/gemini-3.6-flash",)
