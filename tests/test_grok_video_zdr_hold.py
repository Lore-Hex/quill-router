"""xAI's ZDR team cannot serve video until TR supplies output.upload_url.

Since TR's xAI team enabled zero data retention (2026-11-02), xAI requires a
caller-owned output.upload_url on every video generation. The gateway adapter
sends none, so every prepaid x-ai/grok-imagine-video request gets HTTP 400
(Sentry QUILL-ROUTER-F9). The route is held until TR-owned upload storage ships.
"""

from __future__ import annotations

from datetime import date

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import (
    GROK_IMAGINE_VIDEO_HOLD_REASON,
    GROK_IMAGINE_VIDEO_MODEL_ID,
    OPERATOR_HELD_PROVIDER_MODELS,
    provider_model_operator_held,
    provider_route_reviewed_hold,
)

ENDPOINT_ID = f"{GROK_IMAGINE_VIDEO_MODEL_ID}@grok/prepaid"


def test_grok_video_route_is_held_for_provider_access() -> None:
    assert GROK_IMAGINE_VIDEO_MODEL_ID == "x-ai/grok-imagine-video"
    assert GROK_IMAGINE_VIDEO_HOLD_REASON == "provider-access-unavailable"
    assert ("grok", GROK_IMAGINE_VIDEO_MODEL_ID) in OPERATOR_HELD_PROVIDER_MODELS
    assert provider_model_operator_held("grok", GROK_IMAGINE_VIDEO_MODEL_ID, "grok-imagine-video")
    # A reviewed hold, not drift: freshness reports read it as intentional.
    assert provider_route_reviewed_hold("grok", GROK_IMAGINE_VIDEO_MODEL_ID)
    # Only the video route: xAI's chat routes are untouched.
    assert not any(
        provider == "grok" and model_id != GROK_IMAGINE_VIDEO_MODEL_ID
        for provider, model_id in OPERATOR_HELD_PROVIDER_MODELS
    )


def test_grok_video_route_is_out_of_routing_and_catalog() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, endpoint_for_id

    assert endpoint_for_id(ENDPOINT_ID) is None
    assert ENDPOINT_ID not in MODEL_ENDPOINTS
    # It was the model's only route, so nothing routes the model at all.
    assert not [row for row in MODEL_ENDPOINTS.values() if row.model_id == GROK_IMAGINE_VIDEO_MODEL_ID]
    # Other grok routes still serve.
    assert any(row.provider == "grok" for row in MODEL_ENDPOINTS.values())



def test_fully_held_grok_video_model_claims_no_availability() -> None:
    from trusted_router.catalog import MODELS, model_to_openrouter_shape

    model = MODELS[GROK_IMAGINE_VIDEO_MODEL_ID]
    assert (model.prepaid_available, model.byok_available) == (False, False)
    shape = model_to_openrouter_shape(model)["trustedrouter"]
    assert not shape["prepaid_available"]
    assert not shape["byok_available"]


def test_grok_video_hold_survives_explicit_media_route_and_fresh_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registry re-adds this hardcoded video route on every build, and an
    # hourly refresh rebuilds the catalog. Neither may restore it: explicit
    # media routes skip the chat allowlist, but not an operator route hold.
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    control = "google/veo-3.1-fast"
    endpoints = {
        ENDPOINT_ID: ModelEndpoint(
            id=ENDPOINT_ID, model_id=GROK_IMAGINE_VIDEO_MODEL_ID, provider="grok",
            usage_type="Credits", upstream_id="grok-imagine-video",
        ),
        f"{control}@google-vertex/prepaid": ModelEndpoint(
            id=f"{control}@google-vertex/prepaid", model_id=control, provider="google-vertex",
            usage_type="Credits", upstream_id="veo-3.1-fast-generate-001",
        ),
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, explicit_model_ids=frozenset({GROK_IMAGINE_VIDEO_MODEL_ID, control}),
    )
    assert set(filtered) == {f"{control}@google-vertex/prepaid"}


def test_daily_video_canary_never_probes_held_grok_route() -> None:
    from trusted_router.synthetic.video_generation import (
        DAILY_VIDEO_PROFILES,
        daily_video_profile,
        daily_video_profile_held,
    )

    grok = next(profile for profile in DAILY_VIDEO_PROFILES if profile.provider == "grok")
    assert daily_video_profile_held(grok)
    # A full rotation and then some: grok is never chosen, and every chosen
    # profile names a route the catalog still serves.
    from trusted_router.catalog import endpoint_for_id

    for offset in range(30):
        profile = daily_video_profile(date.fromordinal(date(2026, 11, 25).toordinal() + offset))
        assert profile.provider != "grok"
        assert endpoint_for_id(f"{profile.model}@{profile.provider}/prepaid") is not None
